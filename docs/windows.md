# Windows 使用指南

[返回项目首页](../README.md) · [客户端接入](clients.md)

Windows 提供桌面控制台和命令行入口。桌面控制台用于选择工作区、启动 HTTP 服务及管理 Cloudflare Tunnel。

## 下载单文件版

[Windows 单文件版 v0.1.0](https://github.com/DNLINYJ/kohaku-kt-mcp-lite/releases/tag/v0.1.0) 提供以下文件：

- [KT-MCP-path-token.exe](https://github.com/DNLINYJ/kohaku-kt-mcp-lite/releases/download/v0.1.0/KT-MCP-path-token.exe)
- [KT-MCP-path-token.exe.sha256](https://github.com/DNLINYJ/kohaku-kt-mcp-lite/releases/download/v0.1.0/KT-MCP-path-token.exe.sha256)

下载到同一目录后，在 PowerShell 中核对 SHA256：

```powershell
Get-FileHash .\KT-MCP-path-token.exe -Algorithm SHA256
Get-Content .\KT-MCP-path-token.exe.sha256
```

确认哈希一致后，双击 EXE 打开桌面控制台。单文件版面向 Windows x64，内置 Python 运行时。运行 `bash` 工具需要已安装的 Bash，例如 Git for Windows。

## 从源码安装

准备 Python 3.10+，在仓库目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\kt-mcp-gui.exe
```

后续可以双击仓库中的 [start-gui.cmd](../start-gui.cmd)，也可以执行：

```powershell
.\.venv\Scripts\python.exe -m kt_mcp.gui
```

桌面界面使用 Python 自带的 Tkinter。

## 使用桌面控制台

1. 选择 MCP 可以访问的工作目录，填写本机 HTTP 端口。
2. 选择接入方式，点击 **启动服务**。
3. 启动完成后，点击 **复制地址** 或 **复制客户端 JSON（含令牌）**，填入支持 Streamable HTTP 的 MCP 客户端。
4. 使用 **停止** 结束当前服务。关闭窗口时，也会停止该窗口的 HTTP 服务、工具任务和 Tunnel。

| 接入方式 | 配置与地址 |
| --- | --- |
| 临时公网地址 | 默认选项，自动分配 `https://随机名称.trycloudflare.com/mcp/访问令牌` |
| 固定域名 | 填写公网域名和已有的 Cloudflare Tunnel Token |
| 仅本机 HTTP | 使用 `http://127.0.0.1:端口/mcp/访问令牌` |

临时地址由 Cloudflare 分配，重新启动后需要从界面获取连接地址。需要长期使用同一域名时，选择固定域名模式。

`cloudflared` 路径可以留空。程序先查找已有连接器，缺少时从 Cloudflare 官方发布下载并校验 SHA256；也可以在界面选择本机已有的可执行文件。运行状态和连接器输出显示在窗口日志中。

### 固定域名

1. 在 Cloudflare 控制台创建远程管理的 Tunnel，并取得连接器 Token。
2. 将公网域名的服务路由设为 `http://127.0.0.1:8765`，端口与桌面控制台填写的一致。
3. 在桌面控制台选择 **固定域名**，填写 Token，以及形如 `https://mcp.example.com` 的域名。
4. 点击 **启动服务**，复制完整 MCP 地址到客户端。

域名输入框填写 HTTPS 源地址，MCP 路径由程序添加。使用 Cloudflare Access 时，按相应策略为客户端配置认证。

### 设置与令牌

工作区、端口、接入方式、域名和 cloudflared 路径保存到 `%LOCALAPPDATA%\Kohaku KT MCP\desktop.json`。自动下载的连接器位于同目录的 `bin` 子目录。

GUI 每次打开时默认生成新的 MCP 访问令牌。需要使用固定令牌时，可在界面填写，或在启动 GUI 前通过 `KT_MCP_TOKEN` 环境变量提供。Tunnel Token 可以在界面输入，也可以通过 `TUNNEL_TOKEN` 环境变量提供；这两种令牌由当前会话使用，设置文件只保存上述连接选项。

完整连接地址包含 MCP 访问令牌，客户端配置方法见 [客户端接入](clients.md)。

## 命令行使用

源码安装后，可使用虚拟环境中的 `kt-mcp.exe`。例如，为本地 MCP 客户端启动 stdio 服务：

```powershell
.\.venv\Scripts\kt-mcp.exe --transport stdio --workspace 'D:\Projects\my-project'
```

客户端配置中使用 `kt-mcp.exe` 和工作区的实际绝对路径。HTTP 命令、客户端 JSON 及反向代理设置见 [客户端接入](clients.md)。

## 构建单文件 EXE

打包环境为 Windows x64、Python 3.12+ 和带 C++ 构建工具的 Visual Studio。在仓库目录运行：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -e '.[bundle]'
.\.venv-build\Scripts\python.exe scripts/build_onefile.py
```

构建产物为：

- `dist/KT-MCP.exe`：单文件桌面程序。
- `dist/KT-MCP.exe.sha256`：SHA256 校验文件。
- `dist/nuitka/`：编译中间文件和报告。

重新构建前关闭正在运行的目标 EXE。构建脚本使用 Nuitka、Tkinter 插件和 MSVC，并下载、校验及嵌入 CPython 运行时。

单文件版的 `python` 工具首次调用时，将内置运行时释放到应用数据目录的 `runtimes` 子目录，并在独立子进程中执行代码。可用模块包括标准库和工作目录内的模块。源码安装版使用其 Python 环境中安装的依赖。
