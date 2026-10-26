"""Install and supervise the official cloudflared connector."""

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

import httpx

QUICK_URL = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com\b")


def data_directory() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Kohaku KT MCP"
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "kt-mcp"
    )


def release_asset(system: str, machine: str) -> str:
    architecture = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine.lower())
    if not architecture:
        raise ValueError("此系统架构不支持自动下载，请手动指定 cloudflared 路径")
    if system == "Windows":
        return f"cloudflared-windows-{architecture}.exe"
    if system == "Linux":
        return f"cloudflared-linux-{architecture}"
    if system == "Darwin":
        return f"cloudflared-darwin-{architecture}.tgz"
    raise ValueError("此系统不支持自动下载，请手动指定 cloudflared 路径")


def find_cloudflared(explicit: str, log, cancelled: threading.Event) -> str:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ValueError("指定的 cloudflared 文件不存在")
        return str(path)
    installed = shutil.which("cloudflared")
    if installed:
        return installed
    directory = data_directory() / "bin"
    target = directory / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if target.is_file():
        return str(target)
    asset_name = release_asset(platform.system(), platform.machine())
    log("未找到 cloudflared，正在从 Cloudflare 官方 GitHub Release 下载…")
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        response = client.get(
            "https://api.github.com/repos/cloudflare/cloudflared/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "kt-mcp-desktop",
            },
        )
        response.raise_for_status()
        release = response.json()
        asset = next(
            (item for item in release["assets"] if item["name"] == asset_name), None
        )
        if not asset:
            raise ValueError(f"官方发布中没有 {asset_name}，请手动安装 cloudflared")
        digest = asset.get("digest", "") or ""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("官方发布缺少 SHA256 校验值，请手动安装 cloudflared")
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            archive = Path(temporary) / asset_name
            checksum = hashlib.sha256()
            with client.stream("GET", asset["browser_download_url"]) as download:
                download.raise_for_status()
                with archive.open("wb") as output:
                    for chunk in download.iter_bytes(128 * 1024):
                        if cancelled.is_set():
                            raise InterruptedError("下载已取消")
                        checksum.update(chunk)
                        output.write(chunk)
            if checksum.hexdigest() != digest.removeprefix("sha256:"):
                raise ValueError("cloudflared SHA256 校验失败，下载文件已丢弃")
            if asset_name.endswith(".tgz"):
                executable = Path(temporary) / "cloudflared"
                with tarfile.open(archive) as bundle:
                    member = bundle.getmember("cloudflared")
                    if not member.isfile():
                        raise ValueError("cloudflared 压缩包格式错误")
                    with (
                        bundle.extractfile(member) as source,
                        executable.open("wb") as output,
                    ):
                        shutil.copyfileobj(source, output)
            else:
                executable = archive
            executable.chmod(0o700)
            os.replace(executable, target)
    log(f"cloudflared {release['tag_name']} 下载及 SHA256 校验完成")
    return str(target)


class Tunnel:
    def __init__(self, log):
        self.log = log
        self.url = ""
        self.connected = threading.Event()
        self.process = None
        self.reader = None
        self.temporary = None

    def start(self, executable: str, port: int, token: str = ""):
        # Explicit empty config prevents a user's existing named-tunnel config
        # from interfering with a Quick Tunnel. Never alter ~/.cloudflared.
        self.temporary = tempfile.TemporaryDirectory(prefix="kt-mcp-tunnel-")
        config = Path(self.temporary.name) / "empty.yml"
        config.write_text("{}\n", encoding="utf-8")
        command = [executable, "--config", str(config), "tunnel", "--no-autoupdate"]
        environment = dict(os.environ)
        for key in (
            "TUNNEL_TOKEN",
            "TUNNEL_TOKEN_FILE",
            "TUNNEL_URL",
            "TUNNEL_CONFIG",
            "TUNNEL_ORIGIN_CERT",
        ):
            environment.pop(key, None)
        if token:
            # Avoid exposing credentials in the process command line.
            environment["TUNNEL_TOKEN"] = token
            command += ["run"]
        else:
            command += ["--url", f"http://127.0.0.1:{port}"]
        self.process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        def read():
            for line in self.process.stdout:
                match = QUICK_URL.search(line)
                if match:
                    self.url = match.group(0)
                if "Registered tunnel connection" in line:
                    self.connected.set()
                self.log("[CF] " + line.rstrip())

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def check(self):
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError(
                f"cloudflared 已退出（代码 {self.process.returncode}），请检查上方日志"
            )

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            if self.reader:
                self.reader.join(timeout=2)
            self.process.stdout.close()
        if self.temporary:
            self.temporary.cleanup()
