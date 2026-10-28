# Kohaku KT MCP Lite

面向 Linux 的独立 MCP 工具服务，提供文件读写、搜索和命令执行能力。可以为不同项目创建独立实例，在一个终端界面中统一管理工作区、端口、接入方式和后台服务。

项目源自 [KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium)，无需安装完整的 KohakuTerrarium，也不需要配置模型提供商或 API Key。命令为 `kt-mcp`，Python 包名为 `kohaku-kt-mcp`。

## 安装与启动

需要 Python 3.10+。TUI 和后台服务管理需要正在运行的 systemd；安装及日常使用以普通用户执行。以下依赖安装命令适用于 Ubuntu：

```bash
sudo apt-get update
sudo apt-get install -y curl python3 python3-venv bash coreutils ca-certificates
```

安装最新正式版：

```bash
curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/latest/download/install.sh | bash
```

安装器校验发行包并创建独立 Python 虚拟环境。默认安装位置为 `${XDG_DATA_HOME:-$HOME/.local/share}/kt-mcp`，命令链接位于 `$HOME/.local/bin/kt-mcp`。如果提示命令不在 `PATH` 中，按安装器提示设置；默认目录可使用：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

安装完成后，进入希望 MCP 访问的已有项目目录：

```bash
cd -- /path/to/your/project
kt-mcp
```

将示例路径替换为实际目录。首次进入新目录会打开配置向导，填写工作区、端口和接入方式；再次在同一目录运行，会选中已有实例。在其他项目目录运行同一命令，可以创建其他实例。

也可以直接打开统一管理界面，在界面内新建实例：

```bash
kt-mcp tui
```

固定版本、自定义安装目录、手动安装包和源码安装见 [Linux 详细指南](UBUNTU.md)。

## 在 TUI 中管理实例

总览显示各实例的状态、端口、接入方式和工作区。使用上下键选择，Enter 打开详情。总览和详情都可新建实例或打开操作菜单，空列表也可直接新建。

| 入口 | 操作 |
| --- | --- |
| `n` | 新建实例，配置名称、工作区、端口和接入方式 |
| `a` | 打开 **Actions** 完整操作菜单 |
| `b` | 返回实例列表 |
| `s` / `x` / `r` | 启动 / 停止 / 重启所选实例 |
| `o` | 修改所选实例的配置 |
| `u` | 查看或导出完整 MCP 连接地址 |
| `c` | 查看或导出 HTTP 客户端 JSON |
| `l` | 查看日志，可调整条数和跟随输出 |
| `D`（Shift+D）或 Delete | 删除所选实例 |
| `d`（详情页） | 取消自动启动 |
| `?` | 查看帮助 |
| `q` | 返回上层；在总览中退出 |

**Actions** 还提供安装或重建服务、自启动设置、用户 linger、卸载服务、HTTP/stdio 客户端配置导出、实例列表导出、前台会话、服务详情和软件更新。通过 **Open / create configuration** 可以打开指定配置文件，选择用户服务或系统服务；系统服务操作需要管理员授权。

新建实例后可选择立即启动。普通 **Start** 只注册并启动所选服务，不会自动开启自启动或 linger。需要开机启动并在退出 SSH 后继续运行时，在 Actions 中分别启用 **Enable automatic startup** 和 **Enable user linger**。linger 作用于当前用户的服务管理器。

退出 TUI 不会停止 systemd 后台服务；通过 **Run foreground session** 启动的前台会话，则在退出该会话时停止。

### 修改配置

按 `o` 或选择 **Actions → Configure instance**。界面会先说明并确认暂停所选实例，默认选择 No；确认后停止该实例，再进入配置表单。

可以修改工作区、端口、`local` / `quick` / `named` 接入方式，以及固定域名和 Tunnel Token。在保存确认页选择 `advanced`，还可设置 cloudflared 路径和文件工具访问范围。

**保存或取消编辑后，实例都保持停止，需要按 `s` 重新启动。** 在暂停确认前取消则不改变服务状态。其他实例继续运行，已有 MCP 访问令牌保留。

### 删除实例与卸载服务

| 操作 | 服务 | 配置和访问令牌 | 工作区文件 |
| --- | --- | --- | --- |
| **Uninstall service (keep configuration)** | 停止并移除服务 | 保留，实例仍在列表中，可再次启动 | 保留 |
| **Delete instance (remove configuration)** | 停止并移除服务 | 删除，同时移除运行状态和列表记录 | 保留 |

删除可按 `D` 或 Delete，也可从 Actions 选择。确认界面会显示目标实例和配置路径，默认选择 No。删除实例不会删除远端 Cloudflare Tunnel 或 DNS 配置。

## 接入 MCP 客户端

每个实例独立保存工作区、端口、访问令牌和接入配置。不同实例使用不同端口；固定域名也需要分别配置。HTTP 服务监听本机 `127.0.0.1`，公网访问通过 Cloudflare Tunnel 接入。

| 接入方式 | 用途与配置 |
| --- | --- |
| `local` | 仅本机 HTTP，使用 `http://127.0.0.1:端口/mcp/访问令牌` |
| `quick` | 临时公网 HTTPS 地址，由 Cloudflare 分配，重启后可能变化 |
| `named` | 固定域名，填写已有的 Cloudflare Tunnel Token 和公网域名 |

`quick` 和 `named` 由实例管理 cloudflared 连接器；未找到连接器时自动下载并校验。固定域名模式需要先在 Cloudflare 配置 Tunnel 和公网路由，将服务地址指向该实例的 `http://127.0.0.1:端口`。kt-mcp 使用已有配置启动连接器，不代替 Cloudflare 创建账号、Tunnel 或 DNS 路由。

### Streamable HTTP

启动实例后，在 TUI 按 `u` 获取完整地址，或从 **Actions → Export HTTP client JSON** 导出客户端配置。在导出视图中按 `w` 可保存文件。命令行也可以读取：

```bash
kt-mcp instance link backend
kt-mcp instance client-config backend
```

支持 `mcpServers` 格式的客户端可使用以下结构；将 URL 替换为实例的完整地址：

```json
{
  "mcpServers": {
    "kt-mcp": {
      "url": "http://127.0.0.1:8765/mcp/your-access-token"
    }
  }
}
```

完整 URL 包含访问令牌，无需额外的 Authorization 请求头。**持有该地址的客户端可以读写文件并执行命令，请只提供给可信客户端。** 接入 Quick Tunnel 的实例重启后，应重新获取地址。面板显示配置地址不等于公网路由已可用，连接问题可在日志中查看。

### stdio

使用本地管道的客户端，可以从 **Actions → Export stdio client JSON** 获取配置，由客户端启动并管理 MCP 进程，无需注册后台服务。

手动配置时，使用实际的绝对路径，并显式指定 `--transport stdio`：

```json
{
  "mcpServers": {
    "kt-mcp": {
      "command": "/home/your-user/.local/bin/kt-mcp",
      "args": [
        "--transport", "stdio",
        "--workspace", "/absolute/path/to/project"
      ]
    }
  }
}
```

客户端配置位置和格式以所用客户端为准。stdio 的标准输出专用于 MCP 协议，日志写入标准错误。

## 命令行管理

CLI 与 TUI 使用相同的管理操作。需要脚本或自动化时，可以直接使用命令行；以下示例中的工作区必须已经存在，端口必须可用：

```bash
kt-mcp instance create frontend --workspace /srv/frontend --port 8765 --mode local --start
kt-mcp instance create backend --workspace /srv/backend --port 8766 --mode quick --start
kt-mcp instance list
kt-mcp instance stop backend
kt-mcp instance start backend
```

其他常用操作：

| 命令 | 操作 |
| --- | --- |
| `kt-mcp tui --instance backend` | 打开指定实例的管理面板 |
| `kt-mcp instance configure backend` | 暂停实例并修改配置 |
| `kt-mcp instance restart backend` | 重启实例 |
| `kt-mcp instance uninstall backend` | 卸载服务，保留配置 |
| `kt-mcp instance delete backend` | 确认后删除实例，保留工作区文件 |
| `kt-mcp instance list --json` | 导出不含令牌的实例列表 |
| `kt-mcp instance --help` | 查看实例命令帮助 |
| `kt-mcp service --help` | 查看服务、自启动和日志等管理命令 |

非交互脚本删除实例时，必须显式添加 `--yes`。指定配置文件和系统级服务的用法见 [Linux 详细指南](UBUNTU.md)。

## 更新

在 TUI 中打开 **Actions → Software updates**，可以检查更新、选择正式版本、安装更新或恢复中断的更新。更新成功后自动重新加载 TUI。

命令行入口：

```bash
kt-mcp update --check     # 只检查，不安装
kt-mcp update             # 安装最新正式版
kt-mcp update --recover   # 恢复中断的更新
```

自动更新适用于安装器创建的普通用户安装，由原安装用户执行。更新先下载、校验并准备依赖，再暂停关联的运行中用户服务；成功后恢复该服务，原本停止的服务保持停止。配置和访问令牌保留，安装或启动失败时尝试恢复旧环境。

**多个实例共享同一套程序版本。** 更新前先在管理界面停止其他共享该安装的运行实例，再在更新菜单选择关联服务；更新器不会自动停止其他实例。运行中的系统服务也需要先通过系统管理面板停止，更新后由管理员启动。源码安装及旧版安装迁移见 [Linux 详细指南](UBUNTU.md)。

## MCP 工具与权限

| 工具 | 功能 |
| --- | --- |
| `read` | 读取带行号的 UTF-8 文本，支持偏移和行数限制 |
| `write` | 原子创建或替换文件；替换已有文件前需先读取 |
| `edit` | 精确文本替换；匹配不唯一时需明确选择全部替换 |
| `multi_edit` | 对一个文件依次替换，全部成功后一次写入 |
| `glob` | 按相对路径模式查找文件 |
| `grep` | 使用 Python 正则表达式搜索文本 |
| `tree` | 查看有深度和条数限制的目录结构 |
| `bash` | 在工作区运行 Bash 命令 |
| `python` | 在独立 Python 子进程中执行代码，变量不跨调用保留 |
| `job_status` | 查询单个任务或保留的任务列表 |
| `job_wait` | 等待后台任务结果；等待超时不会终止任务 |
| `job_cancel` | 取消任务并清理其进程 |

文件工具默认限制在所选工作区内；修改已有文件前需要先 `read`，文件被外部修改后需要重新读取。`multi_edit` 仅保证单个文件内的原子修改。

**Bash 和 Python 命令没有沙箱隔离，具有服务运行账户的文件系统、环境和网络权限。** 工作区路径限制仅适用于文件工具。建议使用能访问目标项目的普通账户运行服务。

文件工具支持不超过 2 MiB 的 UTF-8 文本，命令和读取输出上限为 64 KiB。单次命令超时上限为 300 秒；后台任务需要通过 `job_status` 或 `job_wait` 收集结果。服务停止会清理其任务进程，任务和文件读取状态不跨服务重启保留。

## 文档与许可

- [Linux 详细指南](UBUNTU.md)：安装选项、服务配置、更新恢复和故障排查。
- [发行版本](https://github.com/Stanwade/kohaku-kt-mcp-lite/releases)：安装包及校验文件。
- [许可证](LICENSE)与 [NOTICE](NOTICE)：许可条款和来源说明。

源自 KohakuTerrarium 2.1.6，版权归 2024–2026 Shih-Ying Yeh（KohakuBlueLeaf）及贡献者所有。遵循 KohakuTerrarium License 1.0，并保留 Kohaku 品牌及来源署名。
