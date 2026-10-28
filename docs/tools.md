# MCP 工具参考

kt-mcp 提供 12 个工作区工具，通过 [MCP 客户端](clients.md) 调用。每个实例使用自己的工作区、文件读取记录和命令任务。

## 工具列表

下表列出调用参数及默认值；未标注默认值的参数为必填项。`job_status` 的 `job_id` 可省略。

| 工具 | 功能 |
| --- | --- |
| `read(path, offset=0, limit=2000)` | 读取 UTF-8 文本并显示行号；`offset` 从 0 开始 |
| `write(path, content)` | 原子创建文件或替换全部内容 |
| `edit(path, old_string, new_string, replace_all=false)` | 精确文本替换 |
| `multi_edit(path, edits)` | 对同一文件依次执行替换，全部成功后写入 |
| `glob(pattern, path=".", limit=200)` | 按相对路径模式查找文件 |
| `grep(pattern, path=".", glob="**/*", ignore_case=false, limit=100)` | 使用 Python 正则表达式搜索文本，返回匹配行及行号 |
| `tree(path=".", depth=3, limit=200)` | 按深度和条数列出目录结构 |
| `bash(command, timeout=60, run_in_background=false)` | 在工作区启动 Bash 子进程 |
| `python(code, timeout=60, run_in_background=false)` | 在工作区启动独立 Python 子进程 |
| `job_status(job_id?)` | 查询指定任务；省略 `job_id` 时列出保留的任务 |
| `job_wait(job_id, timeout=10)` | 等待任务完成或等待时间结束，返回当前状态 |
| `job_cancel(job_id)` | 取消运行中的任务并等待进程清理；已结束的任务保持原状态 |

## 文件读写

相对路径以实例工作区为基准。默认路径范围包含工作区内的文件，符号链接按其最终目标判断；启用工作区外访问（命令行选项 `--allow-outside-workspace`）后可访问其他路径。

修改已有文件前，先调用 `read` 建立内容基线。成功写入也会更新基线；文件随后被其他程序修改时，重新读取后再编辑。同一实例的客户端共享读取基线，文件读写操作按顺序执行。

`edit` 和 `multi_edit` 使用精确字符串匹配。`old_string` 至少匹配一次；有多个匹配时，可增加上下文选中一处，或设置 `replace_all: true` 替换全部。

`multi_edit` 的 `edits` 为 1–100 个替换项，按数组顺序作用于前一项的结果：

```json
{
  "path": "config.txt",
  "edits": [
    {"old_string": "port=8765", "new_string": "port=8766"},
    {"old_string": "mode=local", "new_string": "mode=quick"}
  ]
}
```

所有替换成功后，工具通过原子替换写入该文件。任意替换失败时保留原文件内容。

## 文件搜索

`glob` 与 `tree` 的 `path` 为目录，`grep` 的 `path` 也可以直接指定文件。搜索跳过符号链接及 `.git`、`.venv`、`venv`、`node_modules`、`__pycache__` 目录；忽略规则使用上述固定列表，不读取 `.gitignore`。

路径模式使用 Python `fnmatch` 匹配完整相对路径，`*` 可以匹配 `/`。以 `**/` 开头的模式也匹配搜索根目录中的文件，例如 `**/*.py` 可匹配 `main.py` 和 `src/main.py`。

`grep` 跳过非 UTF-8、二进制、超出文件大小限制或无法读取的文件，并在 `skipped_files` 中返回数量。匹配行显示 1 起始行号；过长行会截断并设置该行的 `truncated` 字段。

## 命令与后台任务

Bash 和 Python 在实例工作区中启动，使用服务运行账户的文件系统、环境和网络权限。文件工具的路径范围设置只作用于文件工具。命令标准输入关闭，标准输出和标准错误合并到 `output`；每次 Python 调用使用新进程，变量不跨调用保留。

默认调用等待命令结束。设置 `run_in_background: true` 时立即返回 `job_id`，随后通过 `job_status` 或 `job_wait` 获取状态和结果。后台执行沿用同一个命令超时设置。

| `state` | 含义 |
| --- | --- |
| `running` | 命令运行中 |
| `completed` | 命令成功结束 |
| `error` | 命令返回非零退出码、超时或执行失败 |
| `cancelled` | 任务已取消 |

`job_wait` 的等待时间结束后，仍在运行的命令继续执行。取消使用 `job_cancel`。后台任务完成后由客户端查询结果；任务状态保存在实例内存中，同一实例的客户端共享任务列表。

服务关闭时清理所属命令及其后代进程；重启后重新建立任务和文件读取状态。任务列表达到保留上限时，按创建顺序移除已结束的任务。

## 结果格式

工具返回结构化 JSON，以及包含相同 JSON 的文本块。命令结果包括 `output`、`exit_code`、`truncated`，执行异常或超时还包含 `error`。

前台命令失败及工具调用错误会设置 MCP `isError`。后台命令的执行结果位于任务的 `result` 字段，完成状态由 `state` 表示。

## 默认限制

| 项目 | 范围或上限 |
| --- | --- |
| 文本文件大小 | 2 MiB，UTF-8 |
| 命令与文件读取输出 | 64 KiB |
| 单次搜索扫描条目 | 10,000 |
| `read` 的 `limit` | 1–10,000 行 |
| `glob` / `grep` / `tree` 的 `limit` | 1–1,000 条 |
| `tree` 的 `depth` | 1–20 层 |
| 命令 `timeout` | 大于 0 且不超过 300 秒 |
| `job_wait` 的 `timeout` | 0–60 秒 |
| 同时运行的命令 | 8 个 |
| 保留的任务 | 64 个 |

输出被截断时，结果带有 `truncated` 标记。读取时可调整 `offset` 和 `limit`，搜索时可缩小 `path` 范围。

[返回项目首页](../README.md)
