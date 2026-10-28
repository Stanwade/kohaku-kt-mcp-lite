# v0.2.3 — Clearer terminal UI and direct MCP links

本次聚焦 Linux 终端体验：引导更简短，重点信息更醒目，配置后直接在主页看到 MCP 连接地址。

## 本次改进

- 向导用颜色和粗体突出当前提示、默认值与确认步骤，保留输入检查、返回修改和取消。
- 紧凑主页默认显示**含 MCP 访问令牌的完整 MCP link**，长地址完整换行，超出页面时可用上下键或 PgUp/PgDn 滚动。
- `u` 打开纯链接视图，并给出带当前 `--config` 路径的准确单行导出命令；`c` 仍可查看客户端 JSON。
- 日志只在按 `l` 时加载，日志和错误继续脱敏，Cloudflare Tunnel Token 不在主页或链接视图中显示。完整路径与管理帮助分别放在 `v` 和 `?`。
- 无色终端自动降级；`NO_COLOR=1 kt-mcp` 关闭颜色，保留终端支持的粗体及默认前景/背景。`q` 只退出面板，不改变服务状态。

local/named 的 `Configured` 地址可提前查看，不代表服务已启动。quick 等待当前服务的有效启动记录，未就绪、停止或记录失效时不沿用旧链接。`Startup verified` / `Tunnel started` 说明启动检查记录匹配当前进程，不是持续公网健康检查。

## 直接获取连接地址

默认配置和独立配置都支持：

```bash
kt-mcp link
kt-mcp link --config /absolute/path/config.json
```

输出只有完整 URL 和一个结尾换行，没有颜色、JSON 或人为折行。quick 未就绪时返回非零状态；local/named 成功导出只说明配置地址已知。需要 JSON 时继续使用 `kt-mcp client-config`，同样支持 `--config`。

## 安装本版本

以普通用户运行安装器。需要 Python 3.10+ 和联网获取依赖；新的 Ubuntu 环境若缺少依赖，先执行：

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates python3 python3-venv bash coreutils
```

固定在线脚本和软件版本安装，外层 `pipefail` 同时检查下载端失败：

```bash
(
  set -o pipefail
  curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/download/v0.2.3/install.sh \
    | bash -s -- --version 0.2.3
)
```

按安装输出设置 PATH，再进入希望 MCP 访问的已有项目目录运行 `kt-mcp`。首次向导按 Enter 接受显示的当前目录，也可输入其他路径；保存后按 `i` 注册并启动用户服务。已有配置直接沿用保存的工作区，切换 shell 目录不会重新配置服务。在线安装本身不创建配置或启动服务。

## 升级已有服务

继续使用原安装位置，先停服务，安装成功后再启动：

```bash
(
  set -euo pipefail
  kt-mcp service stop
  curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/download/v0.2.3/install.sh \
    | bash -s -- --version 0.2.3
  kt-mcp service start
)
```

自定义安装沿用原 `--prefix`、`--bin-dir`、`--python`，服务管理沿用对应的 `--name` / `--system` 和所需权限；安装器仍以普通用户执行。配置、令牌和工作区保留。更换 Python 主/小版本时使用新的安装前缀。完整步骤见 [Ubuntu 指南](https://github.com/Stanwade/kohaku-kt-mcp-lite/blob/v0.2.3/UBUNTU.md)。

## 发布门禁

发行内容继续包括在线入口、Linux 安装包、wheel、源码包和 `SHA256SUMS`。正式发布由现有 CI 门禁控制：三平台回归、真实 systemd 生命周期及全新安装包的 12 工具验收全部成功后，才公开 Release。

目标仍为 Ubuntu x86_64；其他发行版和 ARM64 需另行验收。本次未重建 Windows EXE，LICENSE、NOTICE 和上游归属保持保留。
