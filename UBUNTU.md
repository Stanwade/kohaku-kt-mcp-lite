# Ubuntu / Linux

Ubuntu 22.04/24.04 的无桌面安装使用 Python wheel 和独立虚拟环境，不需要 Tk、图形会话或 tmux。
Windows Nuitka EXE 的内嵌 Python 仅适用于 Windows；Linux 使用虚拟环境自己的 Python 执行 MCP 的 `python` 工具。

## 安装

### Ubuntu 前置依赖

在新的 Ubuntu 环境中执行：

```bash
sudo apt-get update
sudo apt-get install -y curl python3 python3-venv bash coreutils ca-certificates
```

后面的安装命令以普通用户执行，不加 `sudo`。Python 要求 3.10 或以上，首次安装需要联网获取 Python 依赖。

### 在线安装

在终端运行：

```bash
curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/latest/download/install.sh | bash
```

在线脚本从本仓库的最新正式 Release 读取清单，把本次下载固定到一个版本，核对 Linux 发行包和包内文件的 SHA256 后执行包内安装器。安装过程使用独立虚拟环境。

默认安装前缀为 `$XDG_DATA_HOME/kt-mcp`；未设置 `XDG_DATA_HOME` 时使用 `$HOME/.local/share/kt-mcp`。虚拟环境位于前缀下的 `venv`，默认命令链接为 `$HOME/.local/bin/kt-mcp`。安装器只更新自己标记过的虚拟环境，并检查已有同名命令；不会覆盖其他项目的环境或命令入口。

创建命令链接时，如果命令目录尚未加入 `PATH`，在线安装器会打印对应的 `export PATH=...` 命令。在当前终端执行该提示后即可使用 `kt-mcp`；安装器不修改 shell 配置文件。默认命令目录的写法是：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 安装后的首次启动

安装管道不询问工作区或自动打开 TUI。安装完成后，先进入希望 MCP 访问的已有项目目录，再单独启动：

```bash
cd -- /path/to/your/project
kt-mcp
```

将示例路径替换为实际项目目录。`kt-mcp` 按当前目录的真实路径选择实例；该目录还没有实例时，向导创建独立配置，默认工作区就是当前目录，并推荐可用端口。保存前可以检查和修改。安装目录和 MCP 工作目录可以完全不同。在另一个项目目录运行同一命令可以创建第二个实例；已运行的实例继续使用自己的工作区。显式运行 `kt-mcp tui` 则进入统一实例列表。

### 在线安装选项与自动化

给在线安装器传参时使用 `bash -s --`，例如：

```bash
curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/latest/download/install.sh |
  bash -s -- --version 0.3.3 \
    --prefix "$HOME/apps/kt-mcp" \
    --bin-dir "$HOME/.local/bin" \
    --python /usr/bin/python3
```

| 选项 | 行为 |
| --- | --- |
| `--version VERSION` | 安装指定的 `X.Y.Z` 或 `vX.Y.Z`；省略时选择最新正式 Release |
| `--update` | 更新原有托管安装，暂停并恢复原本运行的用户服务 |
| `--name NAME` | 配合 `--update` 选择用户服务；通常可自动识别 |
| `--prefix DIR` | 指定安装前缀，虚拟环境位于 `DIR/venv` |
| `--bin-dir DIR` | 指定 `kt-mcp` 命令链接所在目录 |
| `--python EXEC` | 指定 Python 3.10+ 的可执行路径或 PATH 中的名称；默认 `python3` |
| `--no-link` | 不创建命令链接；使用安装器打印的虚拟环境内绝对命令路径 |
| `--help` | 查看在线安装器帮助 |

`--prefix` 和 `--bin-dir` 的相对路径以运行在线安装命令时的目录为基准。选择 `--no-link` 后，例如安装前缀为 `$HOME/apps/kt-mcp`，可运行 `"$HOME/apps/kt-mcp/venv/bin/kt-mcp" tui`。

在自动化脚本中，启用**外层管道的 `pipefail`**，让下载入口脚本的第一个 `curl` 失败时，整个调用也返回非零：

```bash
(
  set -o pipefail
  curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/latest/download/install.sh |
    bash -s -- --version 0.3.3
)
```

`pipefail` 设置在包含整条管道的 shell 中；仅给接收脚本的那个 `bash` 添加该选项，不能捕获前一个 `curl` 的失败。

### 手动下载发行包

从同一个 Release 下载 `kt-mcp-0.3.3-linux.tar.gz` 和 `SHA256SUMS`，放在同一目录并校验：

```bash
sha256sum --ignore-missing -c SHA256SUMS
```

确认安装包的校验结果为 `OK` 后，解压并运行包内的本地安装器：

```bash
tar -xzf kt-mcp-0.3.3-linux.tar.gz
cd kt-mcp-0.3.3-linux
bash ./install.sh
```

这里的 `install.sh` 安装同目录的 wheel，不再下载 Linux 发行包；Python 依赖仍需联网获取。安装后按前面的首次启动步骤进入实际项目目录。

### 从源码仓库安装

在已有源码 checkout 的仓库根目录执行：

```bash
bash scripts/install_linux.sh
```

`scripts/install_linux.sh` 安装当前 checkout。仓库根目录的 `install.sh` 是在线下载入口；发行包目录里的 `install.sh` 是本地 wheel 安装器，注意命令所在目录。

本地安装器支持 `--prefix`、`--bin-dir`、`--python`、`--no-link`，也可用 `--source` 明确选择本地文件或项目目录：

```bash
bash scripts/install_linux.sh --source /absolute/path/kohaku_kt_mcp-0.3.3-py3-none-any.whl
```

`--source` 只用于本地安装器，在线入口不接受它。

### 升级已有安装

v0.3.1 起，可直接使用 TUI 的 Actions → Software updates。命令行入口（v0.2.4 起）仍可在服务之外的普通用户终端运行：

```bash
kt-mcp update
kt-mcp update --check                  # 只检查，不安装、不暂停服务
kt-mcp update --version 0.3.3           # 指定正式版；不支持降级
kt-mcp update --name kt-mcp-dev         # 选择要恢复运行的用户实例
```

更新器根据**当前执行的 CLI 所属虚拟环境**识别安装，不依赖当前工作目录或后来改变的 XDG 默认目录。它沿用原 Python 和命令链接，不修改配置、访问令牌、unit、自启动或 linger。下载和校验、依赖准备及副本试装均在停服之前完成；随后备份完整旧环境、离线安装并验证。原来运行的用户服务会恢复启动，并通过本机 MCP 初始化核对版本；原来停止的服务保持停止。Quick Tunnel 重启后通常会产生新的公网地址，重新打开 TUI 或运行 `kt-mcp link` 获取。

安装、启动失败或正常中断时会恢复原虚拟环境，包括旧依赖和额外安装的包。恢复失败会保留备份及独立恢复入口；在仍运行的 TUI 中选择 Recover interrupted update，或执行 `kt-mcp update --recover`。若机器断电或进程被强制杀死，可在原安装前缀的 `.kt-mcp-update` 中找到 `worker.py`，用原 base Python 执行 `python3 -I /原安装前缀/.kt-mcp-update/worker.py --recover`（自定义 Python 要换成原解释器）。不要手动删除未完成事务的备份。

**从 v0.2.3 或更早版本首次升级**：旧 CLI 没有 `update`，新在线入口可以直接调用新版更新器，完成相同的服务暂停与恢复：

```bash
curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/latest/download/install.sh \
  | bash -s -- --update
```

自定义安装必须追加原 `--prefix "/原安装目录"`；必要时追加 `--name kt-mcp-dev`。`--update` 保留现有命令链接，因此不与 `--bin-dir`、`--no-link` 同用。第一次迁移完成后，只需 `kt-mcp update`。

自动更新面向安装器创建的普通用户安装。它会拒绝在目标 MCP 自身的工具进程里执行、无法确认 systemd 状态、有未知 unit 覆盖、或有其他共享该安装的运行中实例等情况。多个实例共享一套环境时，先停其他实例，再用 `--name` 选择更新后要启动的用户服务。运行中的**系统级服务**需要管理员先停服，软件更新仍由安装用户执行，之后再由管理员启动；自动更新不会请求 sudo。源码/editable 安装继续通过 Git/pip 更新。

手动安装包仍可在解压目录运行 `bash ./install.sh`；源码安装可在仓库根目录运行 `bash scripts/install_linux.sh`。这两种低层安装方式需自行停服和重启，不提供事务恢复；不要与自动更新同时运行。

升级既有虚拟环境时，Python 主/小版本必须一致；从 3.10 迁移至 3.12 等情况请使用新的 `--prefix`。手动创建、未由安装器标记的环境，可继续用该环境的 pip 升级，或选择新的安装前缀。

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
kt-mcp link
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

用户 unit 位于 `~/.config/systemd/user/kt-mcp.service`（遵循 `XDG_CONFIG_HOME`）。需要多个实例时，在所有管理命令中统一加 `--name kt-mcp-dev` 等 ASCII 名称，并为每个实例选择不同的配置文件和端口。重新生成已有不同内容的托管 unit 使用 `install --replace`；不会覆盖未标记为本项目生成的 unit。手动更新 unit 或软件后，显式运行 `kt-mcp service restart` 让运行中的进程加载新版本；`enable --now` 不会重启已在运行的服务。`kt-mcp update` 已包含其所管理的运行中用户服务的重启。

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

无参数的 `kt-mcp tui` 打开所有用户实例的统一列表。总览与详情都提供 **`n` 新建、`a` 全部操作、`b` 返回实例列表、`D` 删除所选实例**；显式配置入口也可在界面内切换到列表或新建。列表显示名称、状态、端口、接入方式和工作区，上下键选择，Enter 打开详情。空列表同样可使用新建、操作菜单和帮助。详细面板按 `q` 返回上层，列表按 `q` 退出，后台服务继续运行。

`a` **Actions** 提供可滚动的完整菜单：新建、打开/创建显式配置、软件更新、实例列表导出、启停/重启、配置、安装/重建、卸载保留配置、删除实例、自启动、linger、实时日志、链接和 HTTP/stdio 客户端导出、前台会话、服务详情。CLI 与 TUI 共用管理后端，完整映射见 [功能矩阵](docs/TUI_PARITY.md)。

在项目目录直接运行 `kt-mcp` 会选中统一列表中对应目录的实例。新实例向导显示**启动当前命令时所在的目录**，按 Enter 使用它，也可输入其他已有目录。支持绝对路径、相对路径和 `~/项目`；路径中有空格时直接输入，不需要 shell 引号。

每个命名实例保存到 `~/.config/kt-mcp/instances/kt-mcp-名称.json`（遵循 `XDG_CONFIG_HOME`），配套自己的 `kt-mcp-名称.service`、端口、访问令牌和启动状态。自动命名包含目录名和真实路径摘要，同名但路径不同的项目仍能区分。重复进入同一真实目录会找到原实例。已有 `~/.config/kt-mcp/config.json` 自动纳入列表，无需迁移；默认显示为 `kt-mcp`，以前已用自定义服务名注册时沿用实际服务名。

按名称直接选择可运行 `kt-mcp tui --instance backend`。保留显式入口 `kt-mcp tui --config /absolute/path/config.json --name kt-mcp-backend`；显式路径不会被当前目录覆盖。也可在 Actions → Open / create configuration 选择配置路径、服务名和 user/system 作用域。

**修改已有配置按 `o` Configure**。首先明确显示要暂停的实例，默认选择 No；确认后等待该服务停止，再进入预填现有值的向导，其他实例继续运行。可以改工作区、端口、quick/named/local 接入方式；named 模式还可改公网域名和隐藏输入的 Tunnel Token，Enter 保留原值。MCP 访问令牌不重新生成，保存文件保持原属主和 `0600` 权限。

保存会同步更新 systemd 的工作目录并重新加载 unit，不改变自启动和 linger。**编辑期间保持停止，保存或取消后也保持停止；回到面板按 `s` 才启动**，并检查本机 MCP 连接。确认暂停之前取消则不改变服务状态。保存或 unit 更新失败时尝试恢复原配置和 unit；存在外部修改时拒绝覆盖并显示恢复状态。未注册服务时只编辑配置，之后按 `s` 注册并启动，或在 Actions 选择具体安装参数。前台会话需要先在其会话视图按 `q` 停止再编辑；程序不会仅凭一个保存的 PID 结束无法验证归属的外部进程。

向导逐项检查工作区、端口和接入模式，输入有误会说明原因并让你重试。输入 `:back` 返回上一步并保留已填内容，`:q`、Ctrl+C 或 Ctrl+D 取消。全部输入在原生 curses 表单内完成，凭据使用隐藏输入，Esc 取消、Ctrl+U 清空当前字段。新建先询问实例名，留空按最终工作区自动命名。最后显示绝对路径、端口、接入方式及配置位置，Enter 保存；`edit` 从工作区开始修改，`advanced` 配置 cloudflared 路径和文件访问范围。保存冲突会显示原因并保留已填内容。保存后可选择 Start now，默认为 No。确认保存前不会写配置，首次初始化不会覆盖已有配置。固定域名的 Tunnel Token 使用隐藏输入，摘要不显示令牌。

新建保存后选择 Yes 启动，或通过快捷键与 Actions 启停实例时，界面显示当前阶段和等待时间。启动、重启期间按 `q`、Esc 或 Ctrl+C 结束就绪等待并返回管理界面，服务继续启动；可在列表查看状态和日志。就绪检查完成后自动返回原界面。

向导和面板使用颜色与粗体突出提示、默认值、地址和状态。无色终端自动降级；使用 `NO_COLOR=1 kt-mcp` 可关闭颜色，保留终端支持的粗体。面板使用 Python 标准库 curses，不需要 Tk 或桌面环境；通过 SSH 使用时需要分配终端，例如 `ssh -t user@host`。

| 按键 | 操作 |
| --- | --- |
| `n` / `a` / `b` | 新建实例 / 全部操作 / 返回实例列表；总览和详情通用 |
| `i` | 安装/重建表单，分别选择启动、自启动、linger、替换及系统运行账户 |
| `o` | 确认暂停 MCP，编辑工作区、端口及接入方式；保存/取消后保持停止 |
| `s` / `x` / `r` | 启动 / 停止 / 重启；普通启动不隐式开启自启动或 linger |
| `e` / `d` | 启用 / 取消自启动；不改变当前运行状态 |
| `D`（Shift+D）/ Delete | 确认后删除所选实例；保留 workspace 文件；小写 `d` 仍取消自启动 |
| `g` | 启用用户 linger |
| `u` | 查看完整链接，视图内 `w` 保存完整、无折行的私有文件 |
| `l` | 脱敏日志；`f` 跟随/暂停，`n` 选 1–1000 行，`w` 导出 |
| `c` | 查看 HTTP 客户端 JSON，`w` 直接导出；stdio JSON 在 Actions 中选择 |
| `v` | 查看完整工作区、配置和 unit 路径，不显示令牌 |
| `?` | 查看当前页面导航和管理帮助 |
| `q` | 退出面板，不改变后台服务状态 |

紧凑主页显示进程状态、PID、重启次数、工作区、Boot/Linger，以及**含 MCP 访问令牌的完整 MCP link**，并按当前状态提示操作。配置路径和其他细节放在 `v`，日志只在按 `l` 时加载。日志继续脱敏，Cloudflare Tunnel Token 不在主页或链接视图中显示。

local/named 的 `Configured` 地址可在未启动时查看，不表示服务可用。quick 必须等当前服务的有效启动记录，等待、停止、失败或记录失效时不继续显示旧链接。systemd 的 active 只表示进程已启动；`Startup verified` / `Tunnel started` 还要求启动记录与该 unit 的 MainPID 匹配。这是启动时的 MCP 检查和首次隧道连接记录，不是持续健康探测，也不保证公网 DNS、路由或访问可用。普通用户服务应启用自启动，并确认 `Linger: yes`。

最小终端尺寸为 64×18。长链接完整换行，超出页面时用上下键或 PgUp/PgDn 滚动，不省略字符；需要完整链接或 JSON 时，直接在视图中按 `w` 导出，不依赖终端选择复制。工作区长路径在中间省略，完整路径见 `v`。弹窗支持中文和方向键、PgUp/PgDn 滚动，Enter/Esc/q 返回主面板。

系统服务可在 Open / create configuration 中选择 system；普通用户会看到默认拒绝的管理员授权确认，确认后由操作系统的 sudo 提示完成认证，再进入系统面板。创建系统配置时显式选择普通运行账户，配置由该账户写入；安装表单收集 run-as、启动和自启动等选项。权限边界与 CLI 相同，拒绝授权或在认证时 Ctrl+C 会回到界面。Linger 权限不足时也提供明确的授权重试。

### 删除实例与卸载服务

在列表选中实例或进入详情后，按大写 `D`（Shift+D）/ Delete，或选择 **Actions → Delete instance (remove configuration)**。确认页列出所选名称、配置及影响范围，默认为 No；取消不会暂停服务或删除文件。确认后停止、取消自启动并移除该实例的托管服务，再删除所选配置与对应运行状态，回到刷新后的用户实例列表；显式系统面板完成后关闭，不进入 root 的用户实例目录。

**workspace 目录和其中的项目文件始终保留。** 操作不删除其他实例，也不改变账户的 linger 或远端 Cloudflare Tunnel/DNS 配置。删除后原实例名、工作区及端口不再由该配置预留，可以重新创建；实际端口仍须未被其他进程占用。只想暂时移除后台服务时，选择 **Uninstall service (keep configuration)**，其配置与访问令牌仍保留，稍后可直接 Start。

默认实例也可以删除；显式配置和系统作用域使用当前面板所选路径与服务名，系统更改仍要求管理员权限。多个服务共用配置、配置路径发生变化或无法确认进程归属时，删除会报错并保留配置，不会凭一个 PID 结束未知进程。停止或移除服务失败时，不继续删除配置。

## 多实例命令行

```bash
kt-mcp instance create frontend --workspace /srv/frontend --port 8765 --mode local --start
kt-mcp instance create backend --workspace /srv/backend --port 8766 --mode quick --start
kt-mcp instance list
kt-mcp instance stop frontend
kt-mcp instance start frontend
kt-mcp instance restart backend
kt-mcp instance link backend
```

`create` 的工作区默认当前目录，省略 `--port` 会推荐未被其他实例预留、且当前可绑定的端口；省略 `--start` 只保存配置。`--start` 只注册并立即启动；开机自启动和退出登录后的持久运行可在 TUI 详细面板用 `e`、`g` 开启。统一列表和详情首次按 `s` 与 CLI 相同，仅注册并启动。名称可以写 `backend` 或完整的 `kt-mcp-backend`。各实例令牌独立生成，普通列表不输出令牌，`link` 显式输出完整客户端链接。

删除使用同一后端，交互确认默认 No：

```bash
kt-mcp instance delete backend
kt-mcp instance delete backend --yes  # 自动化脚本显式确认
```

显式配置使用 `kt-mcp instance delete kt-mcp-backend --config /absolute/path/config.json`；系统作用域另加 `--system`，以已获授权的管理员身份执行。非交互调用必须传 `--yes`，不会读取管道里的协议输入。删除配置会移除其中的本机访问令牌，但不会撤销远端 Tunnel 或删除工作区；保留的旧客户端配置不会自动改写。

创建、编辑或启动前会检查已有实例的端口及 named 域名冲突；停止的实例仍保留自己的端口配置。建议各 named 实例配置独立 Tunnel，并将公网路由指向各自本机端口。Quick Tunnel 的随机域名由 Cloudflare 分配，重启可能变化，不能在本机任意指定。

多实例共用一套软件安装。先在管理器停止其他共享安装的活跃实例，再打开 Actions → Software updates，选择关联服务、检查或安装；也可选择指定版本、确认取消或 Recover interrupted update。进度和结果都在界面中显示，取消会等待恢复结束，安装/恢复成功会重新加载新版 TUI。完成后在同一管理器启动需要运行的其他实例。它复用 `kt-mcp update [--name NAME] [--version VERSION]` 与 `update --recover`，仍拒绝在其他共享实例运行时替换安装环境。

## 无界面运行与客户端配置

```bash
kt-mcp daemon
```

这是供服务管理器托管的前台进程，复用桌面版的 HTTP / Tunnel 生命周期但不导入 Tk。它处理 SIGTERM/SIGINT，关闭时清理本实例的工具任务及连接器。启动失败或连接器退出时返回非零状态，供服务管理器决定重启。日志写入 stderr 并遮盖令牌。

只获取连接地址时，默认配置使用 `kt-mcp link`，独立配置使用：

```bash
kt-mcp link --config /absolute/path/config.json
```

stdout 只有原始完整 URL 和一个结尾换行，没有颜色、JSON 或人为折行。需要包含访问令牌的客户端 JSON 时仍可运行：

```bash
kt-mcp client-config
```

两种导出命令都支持 `--config`。Quick Tunnel 必须等有效启动记录，未就绪时返回非零状态，重启可能更换地址。固定域名和本机地址可提前导出，成功输出不代表服务正在运行。运行状态写入同目录的 `config.status.json`（同样 `0600`），正常停止后移除。

新增的管理命令仅用于 Linux；Windows 继续使用桌面 GUI。Linux 中仅当完全无参数且 stdin、stdout 都是交互终端时，`kt-mcp` 会直接进入 TUI。通过管道启动的 MCP 客户端继续使用 stdio；使用 PTY 的客户端应明确传入 `--transport stdio`。原来的 `kt-mcp --workspace ...` stdio 和 `--transport http` 用法保持兼容。非交互 `init` 缺少 `--workspace` 时会明确报错，不会读取协议输入。

## 自行构建与验收

在源码目录中创建开发环境，再构建发布包：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python scripts/build_linux.py
.venv/bin/python scripts/verify_linux.py --archive dist/linux/kt-mcp-0.3.3-linux.tar.gz --systemd
```

构建结果在 `dist/linux`，包括 wheel、sdist、Linux 安装包、独立的在线入口 `install.sh` 和外层 `SHA256SUMS`；安装包内另有文件校验清单。独立入口作为 Release 资产提供，包内的同名脚本仍用于安装本地 wheel。构建检查包版本一致性，安装包携带安装器及 LICENSE/NOTICE，不携带本机配置、令牌或虚拟环境。自定义输出目录使用 `--output`；复用已有构建依赖时可选择 `--no-isolation`。

验收会在临时目录重新安装 wheel，使用安装后的命令创建临时工作区，调用全部 12 个 MCP 工具，并检查 Python 工具来自新虚拟环境。`--systemd` 额外验证临时用户 unit、TUI 退出后后台继续运行；结束时移除临时服务和安装目录。当前用户必须能访问 `systemctl --user`。不带 `--systemd` 时只运行独立 daemon 进行协议验收。报告默认写入 `dist/linux-smoke.json`，可用 `--report` 修改。

项目的普通测试使用 `python -m pytest`。真实 systemd 用例需要显式启用：

```bash
KT_MCP_TEST_SYSTEMD=1 .venv/bin/python -m pytest tests/integration/test_systemd_lifecycle.py -q -k independent_lifetime
KT_MCP_TEST_SYSTEMD=1 .venv/bin/python -m pytest tests/integration/test_instances_lifecycle.py -q
```

CI 在 Ubuntu 22.04 / Python 3.10、Ubuntu 24.04 / Python 3.12、Windows / Python 3.12 上运行回归测试，并在 Ubuntu 验证真实用户级/系统级 unit 和全新安装包。推送与包版本匹配的 `v*` 标签后，只有全部测试和安装验收成功，流程才会上传校验过的资产并公开 Release。见 [.github/workflows/ci.yml](.github/workflows/ci.yml)。

已验证目标为 Ubuntu x86_64。其他 Linux 发行版和 ARM64 需要在各自环境中再做验收。
