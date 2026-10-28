# v0.2.4 — One-command updates and configuration inside the TUI

本次增加 `kt-mcp update`，以及 TUI 内的工作区和接入方式编辑。Linux 服务继续由 systemd 管理，退出终端或面板后可持续运行。

## 一条命令更新

```bash
kt-mcp update
kt-mcp update --check
```

更新识别当前 CLI 的安装目录和原 Python，下载并校验固定版本的官方 Release，准备依赖并在副本中试装，然后才暂停正在运行的用户服务。正式安装使用准备好的离线包，完成后重新启动原本运行的服务，并用本机 MCP 初始化检查实际版本；原本停止的服务保持停止。

配置、MCP 访问令牌、unit、自启动和 linger 设置保留。安装或启动失败时，独立运行的更新器恢复完整旧虚拟环境，包括旧依赖及用户额外安装的包。恢复受阻会保留备份，并显示可直接执行的恢复命令。

`--version X.Y.Z` 可指定正式版；不支持自动降级。多实例可用 `--name kt-mcp-dev` 指定要恢复运行的用户服务，其他共享该环境的运行实例需要先停止。自动更新适用于安装器创建的普通用户 Linux 安装；源码/editable 环境通过 Git/pip 更新。运行中的系统级服务由管理员停服和重启，安装仍以原普通用户执行。不要在目标 MCP 自身的工具进程内发起更新。

## 旧版首次迁移

v0.2.3 及更早的 CLI 尚无 `update` 命令，使用新版在线入口迁移一次：

```bash
curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/latest/download/install.sh \
  | bash -s -- --update
```

自定义安装目录追加 `--prefix "/原安装目录"`，需要时追加 `--name kt-mcp-dev`。这条入口复用相同更新器，保留原命令链接；之后使用 `kt-mcp update` 即可。

全新安装仍使用：

```bash
curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/latest/download/install.sh | bash
```

需要固定这次版本时，将 URL 中的 `latest/download` 换成 `download/v0.2.4`，并在 `bash -s --` 后添加 `--version 0.2.4`。

## 在 TUI 中修改配置

主页增加 **`[o] Configure`**：

1. 明确提示 MCP 将暂停，默认 No。
2. 用户确认后，等待当前服务停止，再进入预填现有值的向导。
3. 可修改 workspace、端口、quick/named/local 接入方式，以及固定域名和隐藏输入的 Tunnel Token。
4. 保存时保留 MCP 访问令牌、配置属主与私有权限，并同步 systemd 的工作目录。
5. 保存或取消后保持停止；回到面板按 **`s`** 启动，并核验本机 MCP 连接。

确认暂停前取消不会改变服务状态。配置或 unit 更新失败时尝试恢复原内容，发现外部修改时拒绝覆盖并显示恢复情况。未注册 unit 但检测到手工 daemon 仍运行时，会要求先停止它；不会擅自终止归属不明的进程。

原有颜色/粗体、默认完整 MCP link、`u` 链接视图、`l` 脱敏日志、`NO_COLOR`、小终端滚动及退出后继续后台运行均保留。Quick Tunnel 重新启动后通常会换公网地址，请重新复制主页链接。

## 发布验证

发布门禁保留 Ubuntu 22.04 / Python 3.10、Ubuntu 24.04 / Python 3.12 和 Windows 回归，真实 user/system systemd 生命周期，以及全新 Linux 安装的 12 工具验收。新增普通用户终端配置流程、更新故障恢复测试，以及真实 systemd 下的升级、保持停止、失败恢复和配置编辑验证。

发行资产仍为在线入口、Linux 安装包、wheel、源码包及 `SHA256SUMS`。目标为 Ubuntu x86_64；其他发行版和 ARM64 需另行验收。本次没有重建 Windows EXE。完整说明见 [Ubuntu 指南](https://github.com/Stanwade/kohaku-kt-mcp-lite/blob/v0.2.4/UBUNTU.md)。
