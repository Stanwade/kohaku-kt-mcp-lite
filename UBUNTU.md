# Ubuntu / Linux

Ubuntu 22.04/24.04 的无桌面安装使用 Python wheel 和独立虚拟环境，不需要 Tk、图形会话或 tmux。
Windows Nuitka EXE 的内嵌 Python 仅适用于 Windows；Linux 使用虚拟环境自己的 Python 执行 MCP 的 `python` 工具。

## 从源码安装

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv bash ca-certificates
python3 -m venv "$HOME/.local/share/kt-mcp/venv"
"$HOME/.local/share/kt-mcp/venv/bin/python" -m pip install .
export PATH="$HOME/.local/share/kt-mcp/venv/bin:$PATH"
```

MCP 的 Bash/Python 命令具有服务运行账户的权限。使用能访问目标工作区的普通账户，不要以 root 身份开放 MCP。

## 初始化持久化配置

```bash
mkdir -p "$HOME/workspace"
kt-mcp init --workspace "$HOME/workspace"
```

默认配置为 `${XDG_CONFIG_HOME:-$HOME/.config}/kt-mcp/config.json`，包含稳定的随机 MCP 访问令牌，以 `0600` 权限创建。已有配置不会被覆盖。不要提交这个文件或粘贴其中的令牌到日志。

默认仅监听 `127.0.0.1:8765`。可用 `--port`、`--config` 指定端口和独立配置文件；`--workspace` 必须指向已有目录。

需要临时 Cloudflare Tunnel 时，在初始化命令中添加 `--mode quick`。
固定域名 Tunnel 则先在 Cloudflare 创建连接器，将公网路由指向 `http://127.0.0.1:8765`，再运行：

```bash
read -rsp 'Cloudflare Tunnel token: ' TUNNEL_TOKEN; echo
export TUNNEL_TOKEN
kt-mcp init --workspace "$HOME/workspace" --mode named --public-origin https://mcp.example.com
unset TUNNEL_TOKEN
```

以上初始化方式任选一种。编辑已有配置时保留 `version: 1` 和访问令牌，并保持 `chmod 600`。`KT_MCP_TOKEN`、`TUNNEL_TOKEN` 环境变量可覆盖文件中的令牌；systemd 不会自动继承交互 shell 的环境变量。

## 无界面运行与客户端配置

```bash
kt-mcp daemon
```

这是供服务管理器托管的前台进程，复用桌面版的 HTTP / Tunnel 生命周期但不导入 Tk。它处理 SIGTERM/SIGINT，关闭时清理本实例的工具任务及连接器。启动失败或连接器退出时返回非零状态，供服务管理器决定重启。日志写入 stderr 并遮盖令牌。

显式导出包含访问令牌的客户端 JSON：

```bash
kt-mcp client-config
```

Quick Tunnel 必须等服务启动后才能导出当前公网地址，重启可能更换地址。固定域名和本机地址可提前导出。运行状态写入同目录的 `config.status.json`（同样 `0600`），正常停止后移除。

新增的管理命令仅用于 Linux；Windows 继续使用桌面 GUI。原来的 `kt-mcp --workspace ...` stdio 和 `--transport http` 用法保持兼容。
