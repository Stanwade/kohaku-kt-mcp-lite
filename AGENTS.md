# Repository instructions

## Git identity

For every new commit, use the current machine's effective Git `user.name` and
`user.email` configuration for both author and committer. Verify the effective
identity before committing. Do not override it with a generic assistant/Codex
identity. When preparing your own changes in another environment, transfer the
working-tree changes and create the final commit on this machine.

## Command and TUI parity

Expose user-facing management capabilities through both CLI and TUI. Reuse the
same configuration, service, export, and update backends. Keep global creation
and navigation reachable from every user panel; do not require users to type
shell commands to finish an in-app operation. Preserve cancellation semantics,
explicit authorization for system actions, and identical lifecycle side effects.
Verify important TUI workflows with a real PTY and the installed release package,
including small-terminal operation, rather than checking screen startup alone.
