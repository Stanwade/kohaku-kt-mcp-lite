# 客户端接入

kt-mcp 支持 Streamable HTTP 和 stdio。HTTP 连接已启动的服务；stdio 由 MCP 客户端启动进程并管理其生命周期。

## Streamable HTTP

### 获取连接地址

| 使用入口 | 操作 |
| --- | --- |
| Linux TUI | 启动实例后按 `u`，或选择 **Actions → Export MCP link** |
| Linux CLI | 运行 `kt-mcp instance link 名称` |
| Windows GUI | 启动服务后点击“复制地址”或“复制客户端 JSON（含令牌）” |

Linux TUI 的 **Actions → Export HTTP client JSON** 可查看客户端配置，在导出视图按 `w` 保存到新文件。CLI 对应入口为：

```bash
kt-mcp instance client-config backend
kt-mcp instance client-config backend --output /absolute/path/client.json
```

使用默认配置时可运行 `kt-mcp link` 和 `kt-mcp client-config`；指定配置文件时加 `--config /absolute/path/config.json`。

### 接入方式

| 模式 | 地址与配置 |
| --- | --- |
| `local` | 本机 HTTP：`http://127.0.0.1:端口/mcp/访问令牌` |
| `quick` | Cloudflare 分配的临时 HTTPS 地址，服务重启后重新获取 |
| `named` | 固定 HTTPS 域名，填写已有的 Tunnel Token 和公网域名 |

Linux 实例和 Windows GUI 均可管理 cloudflared 连接器；首次需要连接器时会自动下载并校验，也可指定已有的 cloudflared 路径。

固定域名模式先在 Cloudflare 创建远程管理的 Tunnel，并将公网域名的服务路由指向实例的 `http://127.0.0.1:端口`。在 kt-mcp 中填写连接器 Token 和 `https://mcp.example.com` 形式的域名。启用 Cloudflare Access 时，客户端还需完成对应认证。

### 客户端 JSON

支持 `mcpServers` 格式的客户端可使用以下结构，将 URL 替换为复制得到的完整地址：

```json
{
  "mcpServers": {
    "kt-mcp": {
      "url": "http://127.0.0.1:8765/mcp/your-access-token"
    }
  }
}
```

HTTP 使用完整 `/mcp/访问令牌` 路径鉴权。URL 和导出的 JSON 包含连接凭据，授予客户端调用文件及命令工具的权限；凭据应保存在客户端的私有配置中。

本机与固定域名地址可以在配置后导出，实际连接需要服务已启动。临时地址在当前 Tunnel 就绪后生成。连接状态和启动信息可在 TUI 或 GUI 的日志中查看。

## stdio

stdio 适合由本机 MCP 客户端直接启动服务。客户端配置使用可执行文件和工作区的绝对路径，并在参数中指定 `--transport stdio`。

### Linux

在 TUI 选择 **Actions → Export stdio client JSON**，可生成对应工作区的配置。手动配置示例：

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

### Windows

从源码安装后，使用虚拟环境中的 `kt-mcp.exe`：

```json
{
  "mcpServers": {
    "kt-mcp": {
      "command": "C:\\projects\\kohaku-kt-mcp-lite\\.venv\\Scripts\\kt-mcp.exe",
      "args": [
        "--transport", "stdio",
        "--workspace", "C:\\projects\\workspace"
      ]
    }
  }
}
```

将示例路径替换为本机安装位置和项目目录。客户端的配置文件位置及字段以其支持的格式为准。stdio 标准输出用于 MCP 协议，日志写入标准错误。

## 直接运行 HTTP 服务

需要通过脚本启动前台 HTTP 进程时，先生成访问令牌，再运行服务。

### Linux

以下示例使用默认安装位置；自定义安装时替换 Python 路径：

```bash
export KT_MCP_TOKEN="$("${XDG_DATA_HOME:-$HOME/.local/share}/kt-mcp/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
kt-mcp --workspace /absolute/path/to/project --transport http --port 8765
```

### Windows PowerShell

在已安装依赖的源码目录运行：

```powershell
$env:KT_MCP_TOKEN = & .\.venv\Scripts\python.exe -c 'import secrets; print(secrets.token_urlsafe(32))'
.\.venv\Scripts\kt-mcp.exe --workspace 'C:\projects\workspace' --transport http --port 8765
```

服务监听 `127.0.0.1:8765`，客户端地址为 `http://127.0.0.1:8765/mcp/` 加上生成的令牌。令牌使用 32–128 个 URL 安全字符：`A–Z`、`a–z`、`0–9`、`_` 或 `-`。`--token-env NAME` 可指定读取令牌的环境变量。Ctrl+C 结束前台进程。

使用自己的 HTTPS 反向代理时，加上 `--public-origin https://mcp.example.com`，将完整请求路径转发到本机端口，并在代理日志中遮盖令牌路径。Cloudflare 接入可通过 [Linux 管理界面](../UBUNTU.md) 或 [Windows 桌面界面](windows.md) 配置。

相关文档：[工具参考](tools.md) · [项目首页](../README.md)
