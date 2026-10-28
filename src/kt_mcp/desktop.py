"""Desktop service lifecycle, independent of Tk so it can be tested headlessly."""

import json
import queue
import socket
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn

from kt_mcp.auth import MCPPathAuth
from kt_mcp.config import Config
from kt_mcp.runtime import Runtime
from kt_mcp.server import create_app
from kt_mcp.tunnel import Tunnel, data_directory, find_cloudflared


@dataclass(frozen=True)
class DesktopSettings:
    workspace: str = ""
    port: int = 8765
    mode: str = "quick"
    public_origin: str = ""
    cloudflared: str = ""
    allow_outside_workspace: bool = False

    def validate(self):
        Config(Path(self.workspace))
        if type(self.allow_outside_workspace) is not bool:
            raise ValueError("allow_outside_workspace must be a boolean")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("端口必须为 1–65535 的整数")
        if self.mode not in ("local", "quick", "named"):
            raise ValueError("未知接入模式")
        if self.mode == "named" and not self.public_origin:
            raise ValueError("固定域名模式需要填写 HTTPS 公网域名")


def load_settings(path: Path | None = None) -> DesktopSettings:
    try:
        data = json.loads(
            (path or data_directory() / "desktop.json").read_text(encoding="utf-8")
        )
        return DesktopSettings(
            **{
                key: value
                for key, value in data.items()
                if key in DesktopSettings.__dataclass_fields__
            }
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return DesktopSettings()


def save_settings(settings: DesktopSettings, path: Path | None = None):
    # Tokens intentionally never enter this file.
    path = path or data_directory() / "desktop.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def client_configuration(url: str) -> str:
    return json.dumps(
        {"mcpServers": {"kt-mcp": {"url": url}}},
        ensure_ascii=False,
        indent=2,
    )


class DesktopService:
    def __init__(self):
        self.events = queue.Queue(maxsize=2000)
        self.cancelled = threading.Event()
        self.worker = None
        self.secrets = ()

    @property
    def running(self):
        return self.worker is not None and self.worker.is_alive()

    def emit(self, kind: str, value: str):
        if kind != "url":
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
        try:
            self.events.put_nowait((kind, value))
        except queue.Full:
            if kind != "log":
                self.events.get_nowait()
                self.events.put_nowait((kind, value))

    def start(self, settings: DesktopSettings, token: str, tunnel_token: str = ""):
        if self.running:
            raise ValueError("服务已运行")
        settings.validate()
        MCPPathAuth(None, token=token)
        if settings.mode == "named" and not tunnel_token:
            raise ValueError("请填写 Cloudflare Tunnel Token")
        self.secrets = tuple(value for value in (token, tunnel_token) if value)
        self.cancelled.clear()
        self.worker = threading.Thread(
            target=self._run, args=(settings, token, tunnel_token), daemon=True
        )
        self.worker.start()

    def stop(self):
        self.cancelled.set()

    def _wait(self, ready, tunnel=None, timeout=60):
        deadline = time.monotonic() + timeout
        while not ready():
            if self.cancelled.wait(0.1):
                raise InterruptedError("已取消")
            if tunnel:
                tunnel.check()
            if time.monotonic() >= deadline:
                raise TimeoutError("启动超时，请检查网络和 cloudflared 日志")
        if self.cancelled.is_set():
            raise InterruptedError("已取消")

    def _run(self, settings, token, tunnel_token):
        tunnel = Tunnel(lambda line: self.emit("log", line))
        server = server_thread = listener = None
        try:
            self.emit("state", "正在启动…")
            runtime = Runtime(
                Config(
                    Path(settings.workspace),
                    allow_outside_workspace=settings.allow_outside_workspace,
                )
            )
            origin = settings.public_origin if settings.mode == "named" else ""
            # Validate the origin before creating any external connection.
            app = create_app(
                runtime, token=token, port=settings.port, public_origin=origin
            )
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # Permit immediate POSIX service restart after active HTTP requests.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", settings.port))
            if settings.mode != "local":
                binary = find_cloudflared(
                    settings.cloudflared,
                    lambda line: self.emit("log", line),
                    self.cancelled,
                )
                if self.cancelled.is_set():
                    raise InterruptedError("已取消")
                self.emit("state", "正在连接 Cloudflare…")
                tunnel.start(
                    binary,
                    settings.port,
                    tunnel_token if settings.mode == "named" else "",
                )
                if settings.mode == "quick":
                    self._wait(lambda: bool(tunnel.url), tunnel)
                    origin = tunnel.url
                    app = create_app(
                        runtime, token=token, port=settings.port, public_origin=origin
                    )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=settings.port,
                    access_log=False,
                    log_config=None,
                    timeout_graceful_shutdown=3,
                )
            )
            server_thread = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            server_thread.start()
            self._wait(
                lambda: server.started or not server_thread.is_alive(), timeout=15
            )
            if not server.started:
                raise RuntimeError("HTTP 服务启动失败")
            local_url = f"http://127.0.0.1:{settings.port}/mcp/{token}"
            # Exercise the actual MCP handler with the exact public Host/Origin.
            headers = {"Accept": "application/json, text/event-stream"}
            if origin:
                headers.update({"Host": urlsplit(origin).netloc, "Origin": origin})
            with httpx.Client(trust_env=False, timeout=5) as client:
                response = client.post(
                    local_url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "kt-mcp-desktop", "version": "1"},
                        },
                    },
                )
                response.raise_for_status()
                if "result" not in response.json():
                    raise RuntimeError("MCP 初始化检查失败")
            if settings.mode != "local":
                self._wait(tunnel.connected.is_set, tunnel)
            self.emit("url", origin + "/mcp/" + token if origin else local_url)
            self.emit(
                "log",
                "MCP 初始化与认证检查通过。"
                + ("CF 连接已建立；公网访问还取决于域名路由和网络。" if origin else ""),
            )
            self.emit("state", "运行中")
            while not self.cancelled.wait(0.25):
                if not server_thread.is_alive():
                    raise RuntimeError("HTTP 服务意外退出")
                if settings.mode != "local":
                    tunnel.check()
        except InterruptedError:
            self.emit("log", "启动已取消")
        except Exception as exc:  # noqa: BLE001 -- report worker failures to the UI
            self.emit("error", str(exc))
        finally:
            self.emit("state", "正在停止…")
            try:
                try:
                    tunnel.stop()
                except OSError as exc:
                    self.emit("error", f"Tunnel 清理失败：{exc}")
                if server is not None:
                    server.should_exit = True
                if server_thread is not None:
                    server_thread.join(timeout=12)
                    if server_thread.is_alive():
                        self.emit("error", "HTTP 服务尚未完成关闭，请退出程序后重试")
                if listener is not None:
                    listener.close()
            finally:
                self.emit("url", "")
                self.emit("state", "已停止")
