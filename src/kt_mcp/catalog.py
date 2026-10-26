"""MCP schemas for the minimal standalone KT tool surface."""

from mcp.types import Tool, ToolAnnotations

TEXT = {"type": "string"}
PATH = {"type": "string", "minLength": 1}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 1000}
EDIT = {
    "type": "object",
    "properties": {
        "old_string": {**TEXT, "minLength": 1},
        "new_string": TEXT,
        "replace_all": {"type": "boolean"},
    },
    "required": ["old_string", "new_string"],
    "additionalProperties": False,
}


def _tool(name, description, properties, required=(), read_only=False):
    return Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(readOnlyHint=read_only, openWorldHint=True),
    )


def tools() -> list[Tool]:
    """Advertise text operations, process execution, and retained jobs."""
    result = [
        _tool(
            "read",
            "Read UTF-8 text with line numbers. offset is zero-based; limit defaults to 2000. Required before modifying an existing file.",
            {
                "path": PATH,
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
            },
            ["path"],
            True,
        ),
        _tool(
            "write",
            "Atomically create or replace UTF-8 text. Existing files require a prior read and unchanged contents.",
            {"path": PATH, "content": TEXT},
            ["path", "content"],
        ),
        _tool(
            "edit",
            "Exact text replacement after reading the file. An ambiguous match fails unless replace_all is true.",
            {"path": PATH, **EDIT["properties"]},
            ["path", "old_string", "new_string"],
        ),
        _tool(
            "multi_edit",
            "Apply ordered exact replacements to one file, committing once only if every replacement succeeds.",
            {
                "path": PATH,
                "edits": {
                    "type": "array",
                    "items": EDIT,
                    "minItems": 1,
                    "maxItems": 100,
                },
            },
            ["path", "edits"],
        ),
        _tool(
            "glob",
            "Find files using shell-style matching on relative paths. Skips symlinks and common generated directories; does not parse .gitignore.",
            {"pattern": PATH, "path": PATH, "limit": LIMIT},
            ["pattern"],
            True,
        ),
        _tool(
            "grep",
            "Search UTF-8 files with a Python regular expression. Returns matching lines; skips binary, oversized, and unreadable files.",
            {
                "pattern": TEXT,
                "path": PATH,
                "glob": TEXT,
                "ignore_case": {"type": "boolean"},
                "limit": LIMIT,
            },
            ["pattern"],
            True,
        ),
        _tool(
            "tree",
            "List directory entries to a bounded depth. Skips symlinks and common generated directories.",
            {
                "path": PATH,
                "depth": {"type": "integer", "minimum": 1, "maximum": 20},
                "limit": LIMIT,
            },
            (),
            True,
        ),
    ]
    for name, argument in (("bash", "command"), ("python", "code")):
        result.append(
            _tool(
                name,
                (
                    "Run Bash in the workspace."
                    if name == "bash"
                    else "Run code in a fresh Python subprocess; variables do not persist."
                )
                + " No sandbox. timeout defaults to 60 seconds (maximum 300). run_in_background returns a job_id; poll job_status/job_wait. Output is bounded.",
                {
                    argument: TEXT,
                    "timeout": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 300,
                    },
                    "run_in_background": {"type": "boolean"},
                },
                [argument],
            )
        )
    result.extend(
        [
            _tool(
                "job_status",
                "Get a retained job or list jobs. Results are bounded in memory and lost on restart.",
                {"job_id": PATH},
                (),
                True,
            ),
            _tool(
                "job_wait",
                "Wait up to timeout seconds (0–60, default 10). A wait timeout does not cancel the command.",
                {
                    "job_id": PATH,
                    "timeout": {"type": "number", "minimum": 0, "maximum": 60},
                },
                ["job_id"],
                True,
            ),
            _tool(
                "job_cancel",
                "Cancel a running command and wait for process cleanup. Completed jobs are unchanged.",
                {"job_id": PATH},
                ["job_id"],
            ),
        ]
    )
    return result
