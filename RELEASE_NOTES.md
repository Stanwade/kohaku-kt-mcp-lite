# v0.2.0 — Linux systemd and TUI

Ubuntu / Linux 现在可以作为独立后台 MCP 服务运行。使用普通用户的持久化配置，支持本机 HTTP、Cloudflare Quick Tunnel 和固定域名 Tunnel。退出 TUI 或终端不结束 systemd 托管的服务。

## 新增

- 私密的持久化配置与稳定 MCP 访问令牌；配置和运行态文件权限为 0600。
- `kt-mcp daemon` 无桌面入口，处理 SIGTERM/SIGINT、失败退出与任务清理。
- `kt-mcp service` 注册、启停、重启、自启动、日志、卸载；支持用户级 unit + linger，以及指定普通账户的系统级 unit。
- systemd 故障重启、30 秒停止上限、整个 cgroup 的任务清理。
- curses TUI：首次配置向导、状态、启停、日志、客户端 JSON、自启动和 linger；q 退出后后台服务继续运行。
- Ubuntu 安装器：隔离 venv、受保护的命令链接、保留配置的原地升级、特殊路径安全入口；跨 Python 主/小版本迁移使用新的安装前缀。
- Linux 构建、校验、全新安装验收和 GitHub Actions 发布流程。测试包含真实 MCP 的全部 12 个工具、真实 systemd 和 TUI 退出后的服务存活。

## 下载与安装

下载本 Release 的 `kt-mcp-0.2.0-linux.tar.gz` 和 `SHA256SUMS`，在下载目录校验：

```bash
sha256sum --ignore-missing -c SHA256SUMS
```

校验通过后：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv bash coreutils ca-certificates
tar -xzf kt-mcp-0.2.0-linux.tar.gz
cd kt-mcp-0.2.0-linux
bash install.sh
export PATH="$HOME/.local/bin:$PATH"
kt-mcp tui
```

首次向导完成后按 `i` 注册/启用/启动用户服务并启用 linger，按 `q` 退出界面。
若系统策略不允许普通用户启用 linger，请运行 `sudo loginctl enable-linger "$(id -un)"`，再运行 `kt-mcp service install --now`。

完整说明：[Ubuntu 指南](https://github.com/Stanwade/kohaku-kt-mcp-lite/blob/v0.2.0/UBUNTU.md)。

## 发布内容与边界

- `kt-mcp-0.2.0-linux.tar.gz`：wheel、安装器、使用说明、许可文件和内部校验清单。
- `kohaku_kt_mcp-0.2.0-py3-none-any.whl`：Python 分发包。
- `kohaku_kt_mcp-0.2.0.tar.gz`：源码分发包。
- `SHA256SUMS`：以上三个文件的 SHA256。

Linux 安装采用系统 Python 3.10+ 与独立 venv；首次安装需要网络获取 Python 依赖。后台管理需要 systemd，默认仅监听 loopback。Quick Tunnel 重启可能更换公网地址，稳定地址使用 named 模式。WSL 内的服务随 WSL 虚拟机停止而停止。

本次 Linux 验证面向 Ubuntu x86_64。通用 wheel 可安装到其他受支持的 Python 平台，其他 Linux 发行版和 ARM64 需要另行验证。Windows 原有 stdio / HTTP / GUI 源码入口保留；既有 Windows EXE 来自原发行仓库 v0.1.0，本次没有重建 Windows EXE。

项目继续保留 KohakuTerrarium 品牌、上游链接与原许可；详见 LICENSE 和 NOTICE。
