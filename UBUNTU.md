# Ubuntu / Linux

Ubuntu 22.04/24.04 的无桌面安装使用 Python wheel 和独立虚拟环境，不需要 Tk、图形会话或 tmux。
Windows Nuitka EXE 的内嵌 Python 仅适用于 Windows；Linux 使用虚拟环境自己的 Python 执行 MCP 的 `python` 工具。

## 安装

Ubuntu 系统前置条件：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv bash coreutils ca-certificates
```

以普通用户运行安装器。源码仓库中使用：

```bash
bash scripts/install_linux.sh
export PATH="$HOME/.local/bin:$PATH"
```

安装完成后，先切换到希望 MCP 访问的项目目录，再运行 `kt-mcp` 或 `kt-mcp tui`。首次向导会明确展示该目录作为默认工作区；可以直接回车使用，也可输入其他已有目录。安装目录和工作目录可以完全不同。

在 Linux 发行包解压目录中使用 `bash install.sh`，它会安装包内的 wheel。首次安装需要联网获取 Python 依赖。Python 要求 3.10 或以上，不需要向系统 Python 安装项目依赖。

默认虚拟环境为 `${XDG_DATA_HOME:-$HOME/.local/share}/kt-mcp/venv`，命令链接为 `$HOME/.local/bin/kt-mcp`。安装器先检查依赖、目标环境和命令入口，不会覆盖其他项目的虚拟环境或已有同名命令。它只更新自己标记过的虚拟环境；曾手动创建的环境可继续用该环境的 pip 升级，或选择新的安装前缀。

常用选项：

```bash
bash scripts/install_linux.sh --prefix "$HOME/apps/kt-mcp" --bin-dir "$HOME/.local/bin"
bash scripts/install_linux.sh --source /absolute/path/kohaku_kt_mcp-VERSION-py3-none-any.whl
bash scripts/install_linux.sh --prefix "$HOME/apps/kt-mcp" --no-link
```

`--python` 可指定 Python 解释器。升级既有环境时，Python 主/小版本必须一致；从 3.10 迁移至 3.12 等情况请使用新的 `--prefix`。安装完成后用 TUI 的首次向导建立配置，或按下面的 CLI 步骤初始化。

升级时使用相同的安装前缀，在安装前停止服务，完成后重新启动，让进程完整加载新版代码：

```bash
kt-mcp service stop
bash install.sh
kt-mcp service start
```

源码安装时将中间一行换成 `bash scripts/install_linux.sh`；自定义实例或系统级服务沿用对应的 `--name`、`--system` 参数。配置、令牌和工作区继续沿用。

MCP 的 Bash/Python 命令具有服务运行账户的权限。使用能访问目标工作区的普通账户，不要以 root 身份开放 MCP。

## 初始化持久化配置

只创建配置时，在交互终端运行 `kt-mcp init` 即可进入与 TUI 相同的引导。`kt-mcp init --port 9000` 等显式选项会作为向导中的预填值保留，已有配置不会被覆盖。

脚本调用时明确提供 `--workspace`，这样会直接初始化，不读取交互输入；使用当前目录可写 `kt-mcp init --workspace .`。例如：

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

## 注册 systemd 后台服务

推荐普通用户服务：

```bash
kt-mcp service install --now --linger
kt-mcp service status
kt-mcp client-config
```

`--now` 注册并启用、启动服务。`--linger` 请求 systemd 在该用户未登录时也启动用户管理器，使服务在退出 SSH 后继续运行，并随系统启动。若系统策略拒绝普通用户启用 linger，执行：

```bash
sudo loginctl enable-linger "$(id -un)"
kt-mcp service install --now
```

检查是否生效：

```bash
loginctl show-user "$(id -un)" --property=Linger
systemctl --user is-enabled kt-mcp.service
systemctl --user status kt-mcp.service
```

分别应看到 `Linger=yes`、`enabled` 和活动状态。systemd 的 active 表示进程已启动；Cloudflare 初次下载及连接还可能进行中，通过日志确认 MCP 初始化和连接器已就绪。

常用管理命令：

| 命令 | 行为 |
| --- | --- |
| `kt-mcp service start` | 启动 |
| `kt-mcp service stop` | 停止并清理本服务的任务进程 |
| `kt-mcp service restart` | 重启并重新读取配置 |
| `kt-mcp service enable --now` | 启用自启动并立即启动 |
| `kt-mcp service disable --now` | 取消自启动并立即停止 |
| `kt-mcp service logs --follow` | 持续查看 journal 日志 |
| `kt-mcp service uninstall` | 停止、取消自启动并移除本项目 unit；保留配置和工作区 |

用户 unit 位于 `~/.config/systemd/user/kt-mcp.service`（遵循 `XDG_CONFIG_HOME`）。需要多个实例时，在所有管理命令中统一加 `--name kt-mcp-dev` 等 ASCII 名称，并为每个实例选择不同的配置文件和端口。重新生成已有不同内容的托管 unit 使用 `install --replace`；不会覆盖未标记为本项目生成的 unit。更新 unit 或软件后，显式运行 `kt-mcp service restart` 让运行中的进程加载新版本；`enable --now` 不会重启已在运行的服务。

也支持与传统服务器软件相同的系统级 unit。先以普通账户完成安装和初始化，再执行：

```bash
sudo "$HOME/.local/share/kt-mcp/venv/bin/kt-mcp" service install \
  --system --run-as "$(id -un)" \
  --config "$HOME/.config/kt-mcp/config.json" --now
sudo "$HOME/.local/share/kt-mcp/venv/bin/kt-mcp" service status --system
```

系统级 unit 写入 `/etc/systemd/system/kt-mcp.service`，使用指定普通用户运行，不需要 linger。配置应归该运行用户所有、权限为 `0600`，该用户须能访问安装目录和工作区，并能在配置所在目录写入运行状态；系统模式的管理命令都加 `--system`。不要同时启动同端口的系统级和用户级实例。

服务失败时由 systemd 重启，正常 stop 不重启。unit 使用 `KillMode=control-group` 清理 MCP 任务后代、`TimeoutStopSec=30` 限定退出时间，并设置 `NoNewPrivileges=true` 禁止工具通过 setuid/sudo 提升权限。服务不依赖 TUI、终端或 tmux。

WSL 的 Ubuntu 必须启用 systemd，且 Windows/WSL 虚拟机本身处于运行状态；关闭 WSL 虚拟机后，内部 Linux 服务也会停止。

## TUI 终端管理面板

```bash
kt-mcp tui
```

首次运行且配置不存在时，向导明确显示**启动当前命令时所在的目录**，按 Enter 使用它，也可输入其他已有目录。支持绝对路径、相对路径和 `~/项目`；路径中有空格时直接输入，不需要 shell 引号。不要把安装包解压目录误当工作区：先进入你希望 MCP 访问的项目目录，再启动面板。

已有配置时直接打开面板并沿用保存的工作区；之后在 shell 中切换目录，不会改变该服务的工作区。按 `v` 可核对完整路径，需要独立配置时使用 `--config /absolute/path/config.json`；同时运行多个实例时还需不同的 `--name` 和端口。

向导逐项检查工作区、端口和接入模式，输入有误会说明原因并让你重试。输入 `:back` 返回上一步并保留已填内容，`:q`、Ctrl+C 或 Ctrl+D 取消。最后会显示工作区的绝对路径、端口、接入模式及配置位置，按 Enter 保存；输入 `edit` 可从工作区开始修改。确认前不会写配置或启动服务，已有配置不会被覆盖。固定域名的 Tunnel Token 使用隐藏输入，摘要不显示令牌。

面板使用 Python 标准库 curses，不需要 Tk 或桌面环境。通过 SSH 使用时需要分配终端，例如 `ssh -t user@host`。

| 按键 | 操作 |
| --- | --- |
| `i` | 注册用户服务、启用 linger，并启用/启动服务 |
| `s` / `x` / `r` | 启动 / 停止 / 重启 |
| `e` / `d` | 启用 / 取消自启动；不改变当前运行状态 |
| `g` | 启用用户 linger |
| `l` | 查看可滚动的日志，遮盖已知令牌 |
| `c` | 显式显示含访问令牌的客户端 JSON；需要完整原始 JSON 时用 `kt-mcp client-config`，避免屏幕折行影响复制 |
| `v` | 查看完整工作区、配置和 unit 路径，不显示令牌 |
| `?` | 查看按键帮助、当前实例的 CLI 命令和 linger 权限问题的处理方式 |
| `q` | 退出面板，后台服务继续运行 |

面板显示 systemd 状态、PID、重启次数、工作区、接入模式、日志和 linger 状态，并根据未注册、停止、启动中、失败或运行状态提示下一步。systemd 的 active 表示服务进程已启动，MCP 或 Tunnel 是否就绪仍应查看日志。普通模式应看到 `Linger: yes` 且自启动状态为 `enabled`，才能确保未登录时仍启动用户服务。

最小终端尺寸为 64×18；进程状态与自启动信息分行显示，长路径会在中间省略并保留末尾目录名，按 `v` 查看完整路径。弹窗按终端显示宽度换行，支持中文；日志、详情、帮助和客户端 JSON 窗口支持方向键、PgUp/PgDn，Enter/Esc/q 返回主面板。

多实例使用与安装相同的 `--name` 和 `--config`。管理已安装的系统级服务时使用 `tui --system --config /absolute/path/config.json`，需要相应权限；系统级首次注册仍通过显式指定 `--run-as` 的 CLI 完成。

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

新增的管理命令仅用于 Linux；Windows 继续使用桌面 GUI。Linux 中仅当完全无参数且 stdin、stdout 都是交互终端时，`kt-mcp` 会直接进入 TUI。通过管道启动的 MCP 客户端继续使用 stdio；使用 PTY 的客户端应明确传入 `--transport stdio`。原来的 `kt-mcp --workspace ...` stdio 和 `--transport http` 用法保持兼容。非交互 `init` 缺少 `--workspace` 时会明确报错，不会读取协议输入。

## 自行构建与验收

在源码目录中创建开发环境，再构建发布包：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python scripts/build_linux.py
.venv/bin/python scripts/verify_linux.py --archive dist/linux/kt-mcp-0.2.0-linux.tar.gz --systemd
```

构建结果在 `dist/linux`，包括 wheel、sdist、Linux 安装包和外层 `SHA256SUMS`；安装包内另有校验清单。构建检查包版本一致性，安装包携带安装器及 LICENSE/NOTICE，不携带本机配置、令牌或虚拟环境。自定义输出目录使用 `--output`；复用已有构建依赖时可选择 `--no-isolation`。

验收会在临时目录重新安装 wheel，使用安装后的命令创建临时工作区，调用全部 12 个 MCP 工具，并检查 Python 工具来自新虚拟环境。`--systemd` 额外验证临时用户 unit、TUI 退出后后台继续运行；结束时移除临时服务和安装目录。当前用户必须能访问 `systemctl --user`。不带 `--systemd` 时只运行独立 daemon 进行协议验收。报告默认写入 `dist/linux-smoke.json`，可用 `--report` 修改。

项目的普通测试使用 `python -m pytest`。真实 systemd 用例需要显式启用：

```bash
KT_MCP_TEST_SYSTEMD=1 .venv/bin/python -m pytest tests/integration/test_systemd_lifecycle.py -q -k independent_lifetime
```

CI 在 Ubuntu 22.04 / Python 3.10、Ubuntu 24.04 / Python 3.12、Windows / Python 3.12 上运行回归测试，并在 Ubuntu 验证真实用户级/系统级 unit 和全新安装包。推送与包版本匹配的 `v*` 标签后，只有全部测试和安装验收成功，流程才会上传校验过的资产并公开 Release。见 [.github/workflows/ci.yml](.github/workflows/ci.yml)。

已验证目标为 Ubuntu x86_64。其他 Linux 发行版和 ARM64 需要在各自环境中再做验收。
