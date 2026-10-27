# Kohaku KT MCP Lite

KohakuTerrarium MCP 工具层的**简易独立版（Lite）**：提供 12 个工作区工具、桌面 GUI、Linux TUI / systemd 管理和 Cloudflare Tunnel 自动接入。

Linux / Ubuntu 用户可在 [本仓库 Releases](https://github.com/Stanwade/kohaku-kt-mcp-lite/releases) 下载安装包，按 [Ubuntu 指南](UBUNTU.md) 安装并运行 `kt-mcp tui`。后台服务由 systemd 管理，退出 SSH 或 TUI 后可继续运行。
Windows 既有 v0.1.0 单文件 EXE 仍可从 [原发行仓库](https://github.com/DNLINYJ/kohaku-kt-mcp-lite/releases/tag/v0.1.0) 下载；本仓库 v0.2.0 提供 Linux 安装包、通用 Python wheel 和源码包。
源码安装、客户端连接和自行打包的方法见下文。

A standalone MCP tool server extracted from [KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium).
The directory and executable are named `kt-mcp`; the Python distribution is `kohaku-kt-mcp`.
No KohakuTerrarium installation, LLM provider, API key, Agent, or Studio is required.

## Ubuntu quick start

```bash
sudo apt-get install -y python3 python3-venv bash coreutils ca-certificates
# In a source checkout; for an extracted release bundle, use bash install.sh.
bash scripts/install_linux.sh
export PATH="$HOME/.local/bin:$PATH"
kt-mcp tui
```

首次 TUI 向导建立配置后，按 `i` 注册并启动用户服务、开启 linger；按 `q` 退出面板。
也可使用 CLI：`kt-mcp init --workspace /absolute/workspace`，然后 `kt-mcp service install --now --linger`。
配置、固定域名、系统级 unit、升级及验证说明见 [UBUNTU.md](UBUNTU.md)。

## Quick start

Python 3.10+ is required. From this directory on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\kt-mcp.exe --workspace 'E:\Agent Project\KohakuTerrarium'
```

The default transport is **stdio**. The client launches the server and owns its lifetime.
Starting it manually waits for MCP messages on stdin; it does not present a chat prompt.
Logs go to stderr and stdout is reserved for the protocol.

On Linux/macOS, use `.venv/bin/python` and `.venv/bin/kt-mcp` instead.

## 桌面 GUI 与 Cloudflare Tunnel

安装后运行桌面版（Windows 无终端窗口）：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\kt-mcp-gui.exe
```

也可以双击项目目录中的 [`start-gui.cmd`](start-gui.cmd)，或运行
`.\.venv\Scripts\python.exe -m kt_mcp.gui`。
桌面版使用 Python 自带的 Tkinter，无需额外安装 GUI 库。Linux 的 Python 可能需要通过系统包管理器安装 `python3-tk`。

1. 选择需要开放给 MCP 客户端的工作目录，设置本机 HTTP 端口。
2. 选择接入方式，点击「启动服务」。首次需要 Tunnel 且未安装 `cloudflared` 时，自动从
   [Cloudflare 官方 GitHub Release](https://github.com/cloudflare/cloudflared/releases/latest)
   下载当前平台的可执行文件并核对发布资产的 SHA256。也可指定已有的 `cloudflared` 文件。
3. 启动完成后点击「复制地址」或「复制客户端 JSON（含令牌）」，粘贴至支持
   Streamable HTTP 的客户端。完整地址为 `/mcp/<访问令牌>`，无需额外设置请求头。
4. 点击「停止」或关闭窗口，会关闭本实例的 HTTP 服务、工具任务和 Tunnel。

| 接入方式 | 行为 |
| --- | --- |
| 临时公网地址（默认） | 不需要 CF 账号，自动创建 `https://随机名称.trycloudflare.com/mcp/<访问令牌>`，识别地址并配置精确的 Host/Origin 白名单 |
| 固定域名 | 使用已有的 Cloudflare Tunnel Token 启动连接器，公网域名填写 `https://mcp.example.com`（不含 `/mcp`） |
| 仅本机 HTTP | 不启动 Tunnel，使用 `http://127.0.0.1:端口/mcp/<访问令牌>` |

**固定域名的首次配置：**在 Cloudflare 控制台创建一个远程管理的 Tunnel，复制连接器 Token，
并将公网域名的服务路由配置为 `http://127.0.0.1:8765`（端口与 GUI 一致）。将域名与 Token 填入 GUI 即可。
GUI 自动运行已有连接器；不代替控制台创建账号、Tunnel 或 DNS 路由。若启用 Cloudflare Access，还需在客户端完成相应认证。

临时 Tunnel 重启会更换地址，没有可用性保证，适合测试；固定地址请使用固定域名模式。
参见 [Cloudflare Quick Tunnels 文档](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。
HTTP 服务继续仅监听 `127.0.0.1`，通过完整 URL 路径中的 MCP 访问令牌鉴权；桌面版启动时用实际 MCP 初始化请求检查本机服务和公网 Host/Origin 配置，
并等待连接器注册成功。该检查不保证公网 DNS、Cloudflare Access 或固定域名路由配置正确；状态和连接器输出会显示在日志中。
网络短暂中断时由 `cloudflared` 自行重连；连接器退出时，GUI 报错并停止本实例的 HTTP 服务，可修正配置后重新启动。

非敏感设置保存到 Windows 的 `%LOCALAPPDATA%\Kohaku KT MCP\desktop.json`；自动下载的连接器位于同目录的 `bin`。
Linux/macOS 使用 `$XDG_DATA_HOME/kt-mcp` 或 `~/.local/share/kt-mcp`。
MCP 访问令牌和 Tunnel Token **不写入设置文件**，并会从界面日志中遮盖；客户端连接地址栏会显示含访问令牌的完整 URL。
未设置 `KT_MCP_TOKEN` 时，每次打开 GUI 都会生成新的访问令牌，需要重新复制客户端配置；
也可自行填写稳定的 MCP 令牌，或通过 `KT_MCP_TOKEN` 环境变量提供。
Tunnel Token 可在界面填写，或使用 `TUNNEL_TOKEN` 环境变量预填；运行连接器时通过环境变量传递，不放在进程命令行中。
复制的地址和客户端 JSON 均包含访问令牌。该令牌允许文件修改和命令执行，仅向可信客户端提供。

## Nuitka Windows 单文件打包

使用路径访问令牌的 Windows x64 成品位于 `dist/KT-MCP-path-token.exe`，直接双击启动，不需要在目标电脑安装 Python。
同目录的 `KT-MCP-path-token.exe.sha256` 用于校验文件。构建使用 Nuitka onefile、Tkinter 插件和 MSVC，禁用控制台窗口。
构建脚本默认输出 `dist/KT-MCP.exe`；重新构建前需关闭正在运行的同名 EXE，以便覆盖文件。

重新构建（需要 Windows x64、Python 3.12+ 和带 C++ 工具的 Visual Studio）：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -e '.[bundle]'
.\.venv-build\Scripts\python.exe scripts/build_onefile.py
```

构建脚本从 python.org 下载并按固定 SHA256 校验 CPython 3.13.17 embeddable 运行时，然后封装进同一个 EXE。
单文件版的 `python` 工具首次调用时将该运行时释放到应用数据目录的 `runtimes`，使用独立 Python 子进程执行代码；
支持标准库和工作目录内的 Python 模块，不继承构建机器的虚拟环境或第三方包。
`bash` 工具仍需要目标电脑安装 Bash / Git for Windows；`cloudflared` 保留自动检测、首次下载及校验功能。
编译中间文件与报告保存在 `dist/nuitka`，发布时只需要发送 `dist/KT-MCP.exe`。

打包后的全工具验收可运行 `.\.venv\Scripts\python.exe scripts/verify_onefile.py --executable dist/KT-MCP-path-token.exe`。
省略 `--executable` 时，默认验收构建脚本输出的 `dist/KT-MCP.exe`。
脚本把 EXE 复制到包含空格和中文的临时目录，用独立配置和不含 Python 的 PATH 启动测试窗口。
EXE 的专用 `--verify-onefile` 入口会自动调用同一套 GUI 启停逻辑，脚本通过真实 MCP 客户端测试全部 12 个工具和错误分支，
随后自动关闭测试窗口并检查端口释放。报告保存到 `dist/onefile-smoke.json`。
加上 `--tunnel` 可使用真实临时隧道，通过公网 HTTPS 运行同一套验收，结果保存到 `dist/onefile-smoke-tunnel.json`。
验收需要测试环境安装项目依赖和 Bash / Git for Windows，不会修改日常 GUI 配置或用户工作区。

## Connect a local client

Use the executable's absolute path and an explicit workspace. See
[`examples/client.stdio.json`](examples/client.stdio.json) for an MCP client configuration:

```json
{
  "mcpServers": {
    "kt-mcp": {
      "command": "E:\\Agent Project\\kt-mcp\\.venv\\Scripts\\kt-mcp.exe",
      "args": ["--workspace", "E:\\Agent Project\\KohakuTerrarium"]
    }
  }
}
```

The exact settings location depends on your client. This example uses the common `mcpServers`
JSON format; adapt it to clients with a different configuration format.

## Streamable HTTP

HTTP listens on **127.0.0.1 only** and authenticates the exact `/mcp/<access-token>` path.
Generate a random token and keep it in the server's environment:

```powershell
$env:KT_MCP_TOKEN = & .\.venv\Scripts\python.exe -c 'import secrets; print(secrets.token_urlsafe(32))'
.\.venv\Scripts\kt-mcp.exe --workspace 'E:\Agent Project\KohakuTerrarium' --transport http --port 8765
```

Configure the client to use `http://127.0.0.1:8765/mcp/<the value of KT_MCP_TOKEN>`.
No Authorization header is required. The full URL is the access credential.
Tokens must contain 32–128 URL-safe characters (`A-Z`, `a-z`, `0-9`, `_`, or `-`).
The generated token above meets this format. This MVP uses a pre-shared secret;
it does not implement OAuth discovery or login. Missing or incorrect token paths return HTTP 404.

```json
{
  "mcpServers": {
    "kt-mcp": {
      "url": "http://127.0.0.1:8765/mcp/<your-access-token>"
    }
  }
}
```

For access from another machine, supply your own HTTPS reverse proxy and set
`--public-origin https://your-domain.example` so Host and Origin checks accept that exact origin.
Forward the complete request path to the loopback port. The CLI does not create
or supervise tunnels; the desktop GUI above manages Cloudflare Tunnel automatically.
Server access logs are disabled and the token path is redacted before MCP dispatch.
Keep the full URL out of proxy access logs as well. Stop the foreground HTTP server with Ctrl+C.

## Tools

| Tool | MVP behavior |
| --- | --- |
| `read(path, offset=0, limit=2000)` | UTF-8 text with line numbers; offset is zero-based |
| `write(path, content)` | Atomic creation or full replacement; existing files require a prior read |
| `edit(path, old_string, new_string, replace_all=false)` | Exact replacement; ambiguous matches fail |
| `multi_edit(path, edits)` | Ordered exact replacements in one file; all succeed before one commit |
| `glob(pattern, path=".", limit=200)` | File discovery with shell-style matching against relative paths |
| `grep(pattern, path=".", glob="**/*", ignore_case=false, limit=100)` | Python regex search with line numbers |
| `tree(path=".", depth=3, limit=200)` | Bounded directory listing |
| `bash(command, timeout=60, run_in_background=false)` | Bash subprocess in the workspace |
| `python(code, timeout=60, run_in_background=false)` | Fresh Python subprocess; variables do not persist |
| `job_status(job_id?)` | Get one job or list retained jobs |
| `job_wait(job_id, timeout=10)` | Wait up to 60 seconds; a wait timeout leaves execution running |
| `job_cancel(job_id)` | Cancel a command and await process cleanup |

Results contain structured JSON and a matching text block. Command results include `output`,
`exit_code`, and `truncated`. Foreground failures set MCP `isError`; a background failure is
reported in the job's `state` and `result`. Background jobs have no automatic completion reply.
Use `job_status` or `job_wait` to collect their results.

Existing files require a content baseline from `read` (a successful write also establishes one).
If contents change externally, modifications fail until the file is reread. `multi_edit` is atomic
for one file; it is not a transaction across files. File operations are serialized within an
instance. External writers can still race between the final content check and the atomic rename.

## Scope and limits

- One workspace per process. Start separate processes for separate workspaces.
- File tools resolve relative paths against that workspace and reject paths outside it by default,
  including symlink targets outside it. `--allow-outside-workspace` explicitly relaxes this check.
- **Commands are not sandboxed**: Bash/Python have the server user's filesystem, environment,
  and network permissions. The workspace restriction applies to file tools, not commands.
  A token grants all of these capabilities; use it only with trusted clients.
- File tools support UTF-8 text only, up to 2 MiB per file. No image/PDF/notebook handling.
- Command and read output is capped at 64 KiB. Command timeouts are 0 < timeout <= 300 seconds.
- Search skips symlinks, `.git`, `.venv`, `venv`, `node_modules`, and `__pycache__`, scans at most
  10,000 entries, and does not load `.gitignore`. Narrow the search when the scan limit is reached.
  Glob matching uses Python `fnmatch` on whole relative paths (`*` may match `/`), with `**/`
  also matching root files; it is not a full Git/ripgrep glob implementation.
- Bash requires Bash on PATH; on Windows, Git Bash is preferred. Python uses the server's interpreter.
- Up to 8 concurrent commands and 64 retained jobs. Oldest completed jobs are evicted first.
  Shutdown cancels owned commands; jobs, file read state, and results are lost on restart.
  Clients share that state. Do not retry a side effect automatically after losing its reply.
- No workspace registry, persistent sessions, Agent/subagent delegation, plugins,
  ngrok supervision, or outbound MCP client manager in this MVP.
  The desktop GUI owns one HTTP server and an optional Cloudflare connector while its window is open.
  Linux can instead run the headless daemon under a user or system systemd unit; terminal sessions
  do not own that service. See [Ubuntu service management](UBUNTU.md).

## Extraction map

This is a reduced standalone implementation, not a drop-in replacement for `kt mcp-serve`.
The original repository remains unchanged.

| KohakuTerrarium source | Standalone equivalent |
| --- | --- |
| `api/mcp_tools.py` | `server.py`, `catalog.py`: MCP tool listing, dispatch, HTTP composition |
| `api/auth/mcp_secret.py` | `auth.py`: exact secret-path gating at `/mcp/<access-token>` |
| `mcp_server/runtime.py`, `core/executor.py` | `runtime.py`, `jobs.py`: minimal direct dispatch and job ownership |
| `builtins/tools/{read,write,edit,multi_edit}.py`, `utils/file_guard.py` | `files.py`: text tools, content guards, atomic replacement |
| `builtins/tools/{glob,grep,tree}.py` | `search.py`: bounded file discovery |
| `builtins/tools/{bash,python,bash_windows}.py` | `processes.py`: subprocesses and Git Bash discovery |
| `cli/mcp_serve.py`, `serving/mcp.py` | `cli.py`: foreground serving with stdio or authenticated HTTP |

The MCP transport stays on the [official SDK's v1 API](https://py.sdk.modelcontextprotocol.io/v1/)
with `mcp<2` pinned, matching the KT source generation. Runtime dependencies are the MCP SDK,
Starlette, Uvicorn, and HTTPX, plus their transitive dependencies. The desktop GUI uses Tkinter.

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m black --check src tests
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m build
```

Unit tests cover guards, search limits, jobs, processes, authentication, schemas, and CLI validation.
Integration tests launch the actual CLI and use the official MCP client over stdio and HTTP.
They require no model or credentials and use only temporary workspaces.

## License and attribution

Derived from KohakuTerrarium 2.1.6, copyright 2024–2026 Shih-Ying Yeh (KohakuBlueLeaf) and contributors.
Licensed under the [KohakuTerrarium License 1.0](LICENSE). See [NOTICE](NOTICE) for the standalone changes.
The project retains Kohaku branding and visible attribution to
[KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium).
