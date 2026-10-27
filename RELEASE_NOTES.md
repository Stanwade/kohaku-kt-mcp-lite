# v0.2.2 — One-command Linux installation

Ubuntu / Linux 现在可以直接从网络安装，不再需要手动下载和解压 Release：

```bash
curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/latest/download/install.sh | bash
```

以普通用户运行。首次使用的 Ubuntu 若缺少依赖，先执行：

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates python3 python3-venv bash coreutils
```

需要 Python 3.10+。安装使用独立 venv，仍需联网获取 Python 依赖；安装器不会自动运行 sudo。

## 本次改进

- 独立发布在线 `install.sh`，`latest/download/install.sh` 提供固定入口；支持 `--version X.Y.Z` / `vX.Y.Z` 安装指定正式版本。
- 默认只获取一次最新发布清单，随后使用其中的版本下载固定标签下的安装包，避免发布更新时混用不同版本。
- 校验发行包 SHA256、归档路径与文件类型，以及包内全部文件的校验值，通过后才运行包内安装器。
- 支持 `--prefix`、`--bin-dir`、`--python` 和 `--no-link`，正确处理中文、空格和相对路径；相对路径始终以执行在线安装命令时的目录为基准。
- 实际安装在已校验的私有临时目录中进行，避免项目里的 `json.py`、`venv.py` 或继承的 Python 路径变量干扰旧安装器。
- 下载失败、包损坏或依赖缺失时给出明确错误并清理临时下载；缺少 venv 时显示可复制的 Ubuntu 修复命令。
- 安装结束提示实际命令路径；命令目录不在 PATH 时给出对应的 `export PATH=...`，不会改写 shell 配置。
- 在线脚本作为独立 Release 资产纳入外层 `SHA256SUMS` 和源码包；Linux bundle 内的 `install.sh` 继续用于本地 wheel 安装。

SHA256 用于校验下载内容与发布清单的一致性。管道命令会直接执行下载的在线脚本，不代表先验证该脚本的签名。

## 固定版本与自定义安装目录

同时固定在线脚本和软件版本：

```bash
curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/download/v0.2.2/install.sh \
  | bash -s -- --version 0.2.2 --prefix "$HOME/apps/kt-mcp" --bin-dir "$HOME/.local/bin"
```

默认安装环境为 `${XDG_DATA_HOME:-$HOME/.local/share}/kt-mcp/venv`，命令链接为 `$HOME/.local/bin/kt-mcp`。安装目录与 MCP 工作目录是两回事。

在自动化脚本中，让外层 shell 也检查管道的下载端失败：

```bash
(
  set -o pipefail
  curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/download/v0.2.2/install.sh \
    | bash -s -- --version 0.2.2
)
```

## 安装之后

按安装输出设置当前 shell 的 PATH，然后进入希望 MCP 访问的项目目录，运行 `kt-mcp` 或 `kt-mcp tui`。首次向导按 Enter 接受显示的当前目录，也可输入其他已有目录；保存前支持返回修改或取消。

保存配置后，TUI 中按 `i` 注册并启动用户服务、启用 linger，按 `q` 退出面板。后台运行继续由 systemd 管理。在线安装不会创建配置、弹出 TUI 或自动启动服务。

## 从已有 Linux 安装升级

继续使用原来的安装前缀；如果已经注册并运行服务，先停止，安装成功后再启动：

```bash
(
  set -euo pipefail
  kt-mcp service stop
  curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/download/v0.2.2/install.sh \
    | bash -s -- --version 0.2.2
  kt-mcp service start
)
```

自定义安装继续传入原 `--prefix` / `--bin-dir`，服务管理继续使用对应的 `--name` / `--system` 参数。配置、令牌和工作区会保留。安装器只更新自己标记的 venv；更换 Python 主/小版本时使用新的安装前缀。

手动下载、校验、解压安装和源码安装仍可使用，完整步骤见 [Ubuntu 指南](https://github.com/Stanwade/kohaku-kt-mcp-lite/blob/v0.2.2/UBUNTU.md)。

## 发布内容与验证范围

| 资产 | 用途 |
| --- | --- |
| `install.sh` | 在线安装入口，可通过 curl 管道交给 Bash |
| `kt-mcp-0.2.2-linux.tar.gz` | wheel、本地安装器、使用说明、许可文件和内部校验清单 |
| `kohaku_kt_mcp-0.2.2-py3-none-any.whl` | Python 分发包 |
| `kohaku_kt_mcp-0.2.2.tar.gz` | 源码分发包，包含在线安装器 |
| `SHA256SUMS` | 以上四个资产的 SHA256 |

新增 16 项离线管道安装用例，覆盖版本选择、目标路径、Python 环境隔离、stdin、下载与校验失败、归档边界、前置依赖和清理行为。另已使用公开 v0.2.1 发布包完成真实网络安装，验证向后兼容。

发布门禁继续覆盖 Ubuntu 22.04 / Python 3.10、Ubuntu 24.04 / Python 3.12、Windows / Python 3.12，以及真实 systemd、全新安装包和全部 12 个 MCP 工具。全部检查通过后才公开 Release。

验证面向 Ubuntu x86_64；其他发行版和 ARM64 仍需另行验收。本次没有重建 Windows EXE。WSL 服务仍受 WSL 虚拟机生命周期约束。LICENSE、NOTICE 和上游归属保持保留。
