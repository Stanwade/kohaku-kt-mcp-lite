# v0.2.1 — Terminal onboarding and clearer service management

本次更新完善 Ubuntu / Linux 的首次配置与终端操作体验。工作目录明确默认为启动命令时所在目录，也可输入其他已有路径；错误输入可以原位修正，保存前可以返回修改或取消。

## 改进

- 共享首次向导：支持当前目录、相对路径、绝对路径和 `~`；逐项验证目录、端口、接入模式与固定域名。
- `:back` 返回并保留有效输入，`:q` / Ctrl+C / EOF 取消；最终摘要隐藏令牌，只有确认后才保存私密配置，不覆盖已有配置或自动启动服务。
- `kt-mcp init` 在交互终端可直接进入向导，显式端口、模式、域名和 cloudflared 路径作为预填值保留；明确提供 `--workspace` 时继续无交互初始化。
- Linux 双 TTY 下直接运行无参数 `kt-mcp` 会进入 TUI；安装器提示先进入目标项目目录，避免误选安装包目录。
- 面板根据未注册、停止、启动中、失败或运行状态提示下一步，并单独提示自启动 / linger；不会把 systemd active 当成 MCP 已就绪。
- 64×18 窄终端布局，中文显示宽度处理，长路径中间省略；`v` 查看完整详情，`?` 查看帮助、当前实例的命令和 linger 权限恢复步骤。
- 修复轮询失败后显示旧运行状态，以及关闭只读弹窗后残留 `Working...` 的反馈问题。
- 新增真实 PTY 验收，覆盖首次配置、纠错、取消、隐藏输入、裸命令、交互 init，以及窄屏中文详情与帮助。

## 启动兼容性

通过管道启动的 MCP 客户端继续使用 stdio。显式服务器参数和 Windows 入口保持原行为。

使用 PTY 启动且不给参数的 Linux MCP 宿主会触发交互 TUI；这类宿主应明确使用：

```bash
kt-mcp --transport stdio
```

脚本创建配置时明确提供工作目录，不会读取交互输入：

```bash
kt-mcp init --workspace .
```

已有配置会继续沿用其保存的工作区，升级后不会重新弹出首次向导；切换 shell 目录也不会自动重配已注册服务。面板中的 `v` 可以核对完整路径。

## 下载与安装

下载本 Release 的 `kt-mcp-0.2.1-linux.tar.gz` 和 `SHA256SUMS`，在同一下载目录校验并解压：

```bash
sha256sum --ignore-missing -c SHA256SUMS
tar -xzf kt-mcp-0.2.1-linux.tar.gz
cd kt-mcp-0.2.1-linux
```

首次安装：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv bash coreutils ca-certificates
bash install.sh
export PATH="$HOME/.local/bin:$PATH"
```

然后进入希望 MCP 访问的项目目录，运行 `kt-mcp` 或 `kt-mcp tui`。首次向导按 Enter 可接受显示的当前目录，也可输入其他已有目录。保存后按 `i` 注册并启动用户服务、启用 linger，按 `q` 退出面板；后台服务由 systemd 继续管理。

若系统策略不允许普通用户启用 linger，运行 `sudo loginctl enable-linger "$(id -un)"`，再运行 `kt-mcp service install --now`。

## 从 v0.2.0 升级

在本次发行包解压目录中，沿用原安装前缀：

```bash
kt-mcp service stop
bash install.sh
kt-mcp service start
kt-mcp tui
```

自定义安装沿用原 `--prefix`；自定义实例和系统级服务沿用相应的 `--name` / `--system` 参数。配置、工作区与令牌会保留。完整说明：[Ubuntu 指南](https://github.com/Stanwade/kohaku-kt-mcp-lite/blob/v0.2.1/UBUNTU.md)。

## 发布内容与验证范围

- `kt-mcp-0.2.1-linux.tar.gz`：wheel、安装器、使用说明、许可文件和内部校验清单。
- `kohaku_kt_mcp-0.2.1-py3-none-any.whl`：Python 分发包。
- `kohaku_kt_mcp-0.2.1.tar.gz`：源码分发包。
- `SHA256SUMS`：以上三个文件的 SHA256。

Linux 安装使用 Python 3.10+ 与独立 venv，首次安装需要联网获取依赖。CI 覆盖 Ubuntu 22.04 / Python 3.10、Ubuntu 24.04 / Python 3.12、Windows / Python 3.12，以及真实 systemd、全新安装包和全部 12 个 MCP 工具。发布仅在所有检查通过后公开。

验证面向 Ubuntu x86_64；其他发行版和 ARM64 仍需另行验收。本次没有重建 Windows EXE。WSL 内的服务仍受 WSL 虚拟机生命周期约束。LICENSE、NOTICE 和上游归属保持保留。
