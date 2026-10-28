# Kohaku KT MCP Lite

独立的 MCP 工作区工具服务，源自 [KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium)。为 MCP 客户端提供文件读写、项目搜索、Bash 和 Python 执行能力，支持 Linux 终端管理与 Windows 桌面使用。

## 主要功能

- **12 个工作区工具**：文件读写与编辑、目录搜索、命令执行和后台任务管理。
- **多种接入方式**：stdio、Streamable HTTP，以及 Cloudflare 临时隧道或固定域名。
- **Linux 多实例管理**：通过 TUI 或 CLI 配置各实例的工作区、端口和连接，管理 systemd 服务与软件更新。
- **Windows 桌面界面**：选择工作目录、启动服务、管理 Tunnel，并复制客户端连接配置。

## 快速开始

### Linux

环境需要 Python 3.10+、venv、curl、Bash 和 systemd，依赖安装见 [Linux 指南](UBUNTU.md)。

```bash
curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/latest/download/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
kt-mcp tui
```

在界面中按 `n` 新建实例，填写工作区、端口和接入方式；按 `a` 打开操作菜单。软件更新位于 **Actions → Software updates**。

### Windows

下载 [Windows 单文件版](https://github.com/DNLINYJ/kohaku-kt-mcp-lite/releases/tag/v0.1.0)，双击启动。选择工作目录和接入方式后，启动服务并复制 MCP 地址到客户端。

源码安装、桌面设置和打包方法见 [Windows 指南](docs/windows.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [Linux 指南](UBUNTU.md) | 安装、TUI、多实例、systemd、更新与恢复 |
| [Windows 指南](docs/windows.md) | 单文件版、源码安装、桌面界面与打包 |
| [客户端接入](docs/clients.md) | HTTP、stdio、Cloudflare 和客户端配置示例 |
| [工具参考](docs/tools.md) | 工具参数、文件操作、后台任务与运行范围 |
| [发行版本](https://github.com/Stanwade/kohaku-kt-mcp-lite/releases) | 安装包与版本说明 |

## 许可证

采用 [KohakuTerrarium License 1.0](LICENSE)，来源与修改说明见 [NOTICE](NOTICE)。

Copyright 2024–2026 Shih-Ying Yeh（KohakuBlueLeaf）及贡献者。
