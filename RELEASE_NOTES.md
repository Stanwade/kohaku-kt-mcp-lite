# v0.3.1 — Complete management inside the TUI

修复统一管理入口与详情页之间的新建断点，并将管理操作接到 CLI/TUI 共用后端。

## 界面内完成管理

- 总览与详情都显示 `n` New、`a` Actions 和 `b` Instances；项目目录启动优先选中统一列表中的对应实例。
- 原生 curses 新建与配置表单支持自定义名称、workspace、端口、接入方式及隐藏 Token；Review 的 advanced 可配置 cloudflared 和文件访问范围。冲突后保留输入，取消不创建配置。
- Actions 提供安装/重建参数、启停、自启动、linger、卸载保留配置、显式配置路径、系统作用域及明确的管理员授权。
- 日志可持续跟随、选择行数并导出；链接、HTTP/stdio 客户端 JSON 可以直接保存为私有文件，无需复制终端命令。
- 软件更新菜单支持检查、指定版本、选择关联服务、安装、确认取消、恢复和成功后重载新版界面；复用现有事务更新器。

## 一致的行为

- 普通 Start 与 `instance start` 都只注册并启动，不隐式启用自启动或 linger。完整安装选项单独选择；`install --now` 仍遵循 CLI 的 enable+start 语义。
- 生命周期、自启动及卸载验证所选配置与 unit 绑定；CLI `service --name` 未传配置时沿用 unit 的真实配置路径。
- CLI 增加 `instance configure`、`service configure`、客户端 JSON/stdio 与私有文件导出，以及 `update --recover`。
- 修正 Windows 配置快照的时间戳比较，保留外部修改和文件替换检测。
- 配置前确认暂停选中实例，保存或取消后保持停止，其他实例继续运行。共享安装仍须先在管理器停止其他活跃实例再更新。

## 验证

增加真实 64×18 PTY 新建、冲突保留、隐藏输入、取消、导出和双服务管理验收；发行包验收实际通过安装后的 launcher 完成新建。保留跨平台回归、全部 MCP 工具、真实 systemd、升级与失败回滚门禁。更新输出压力测试确认大量长日志不会阻塞主界面。

---

# v0.3.0 — Independent workspaces and one instance manager

每个项目目录可使用独立的 kt-mcp 实例，拥有自己的 workspace、端口、MCP 令牌、接入方式和 systemd 服务。

## 使用方式

- 在项目目录运行 `kt-mcp`，选择该真实路径的已有实例；新目录进入独立实例向导。
- 运行 `kt-mcp tui` 打开统一列表，查看各实例的状态、端口、接入方式和工作区。
- 上下键选择，Enter 查看详情；`s / x / r` 启动、停止、重启，`o` 配置，`u` 查看链接，`l` 查看日志，`n` 新建。
- 配置前明确提示并确认暂停选中实例；保存或取消后保持停止，按 `s` 再启动，其他实例继续运行。
- 命令行支持 `kt-mcp instance create/list/start/stop/restart/link`。例如：

```bash
kt-mcp instance create frontend --workspace /srv/frontend --port 8765 --mode local --start
kt-mcp instance create backend --workspace /srv/backend --port 8766 --mode quick --start
kt-mcp tui
```

## 配置与兼容

新实例保存到用户配置目录下的 `kt-mcp/instances/`。旧默认配置、旧的显式 `--config / --name` 入口以及非交互 stdio 客户端继续可用。实例创建使用独立随机 MCP 令牌；现有实例编辑保留自身令牌。

保存与启动前检查端口及固定域名冲突；停止的实例继续保留自己的端口。Quick Tunnel 的地址由 Cloudflare 分配，固定地址使用每个实例自己的 named 配置和对应公网路由。

统一列表管理当前普通用户的实例。系统级服务继续通过显式 `--system --config` 入口管理。多实例共享安装时，更新前需先停止其他共享实例，再用 `kt-mcp update --name kt-mcp-名称` 指定待恢复的实例。

## 验证范围

增加实例目录发现、旧配置兼容、端口/域名冲突、并发保存及列表动作路由测试；真实 systemd 验收同时运行两个实例，检查文件与令牌隔离、配置暂停、取消、独立重启及退出统一面板后的后台运行。保留已有跨平台回归、发布包安装和更新回滚门禁。

---

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
