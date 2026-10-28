# CLI and terminal management parity

This document maps the public management commands to their terminal interface
entry points. The instance overview and the instance detail page both expose
`n` (new instance), `a` (all actions), `b` (all instances), and uppercase
`D` / Delete (delete the selected instance). Lowercase `d` still disables
automatic startup. The actions
menu scrolls, so its commands remain reachable at the supported 64 × 18 terminal
size. Native forms accept Enter, `:back`, and `:q`; Esc cancels and Ctrl+U
clears the current input. Named-tunnel secrets are masked.

## Audit context

In 0.3.0, new-instance creation was reachable only from the overview. The
current-directory and explicit-configuration routes could open a detail page
with no new-instance key. Creation and configuration also temporarily left
curses for line-based input. An unregistered instance started through the TUI
used an install path that also enabled automatic startup and user linger,
whereas the CLI instance start command did not. The parity work gives these
entry points common navigation, native forms, and shared management operations.

## Command map

The command map covers Linux configuration, service management, client exports
and updates. TUI entries operate on the selected configuration; CLI commands
select it through a name or explicit path. The verification record identifies
the behavior exercised through a real terminal.

| Public CLI operation | Terminal entry | Shared implementation or relevant behavior |
| --- | --- | --- |
| `kt-mcp tui`; `instance list` | All instances; `b` from either screen | `instances.list_instances`; each row selects an independent saved configuration. |
| `instance list --json` | Actions → Export instance list | Public metadata only; tokens are excluded. |
| Bare `kt-mcp` in an interactive Linux terminal | Current-directory selection or native new-instance form | Workspace lookup uses the same instance catalog. Noninteractive stdio behavior remains explicit through `--transport stdio`. |
| `tui --instance NAME`; `tui --workspace PATH` | Select the instance in the overview | The selected name determines the target of subsequent actions. |
| `instance create NAME --workspace PATH` | `n`; Actions → New instance | Name, workspace, port and connection mode are collected in curses; saving does not start by default. |
| `instance create --port --mode --public-origin --cloudflared --allow-outside-workspace` | New instance; advanced configuration at the save review | The shared configuration wizard validates values and the instance catalog checks reserved workspaces, ports and named origins. |
| `instance create --start` | Start instance now? after save | Default is No. Accepting Yes starts the new instance without enabling automatic startup or linger. |
| `instance configure NAME`; `service configure --config PATH --name NAME [--system]` | `o`; Actions → Configure instance | Shared pause, edit, save/cancel workflow. Changing configuration requires the affected service to remain stopped. |
| `init --config PATH` and its workspace/port/mode/origin/cloudflared/access options | Actions → Open / create configuration | Explicit configuration creation uses the same configuration validation and private-file format. Existing configurations are opened for management. |
| `tui --config PATH --name NAME [--system]` | Actions → Open / create configuration | Explicit path, service name and user/system scope are selectable without losing global navigation. |
| `instance start NAME`; `service start` | `s`; Actions → Start instance | Shared verified lifecycle operation. An instance's known saved configuration can be registered on first Start; a service-only command requires an installed unit. No implicit enable or linger. |
| `instance stop NAME`; `service stop` | `x`; Actions → Stop instance | Shared verified binding and external-process guard. Only the selected service stops. |
| `instance restart NAME`; `service restart` | `r`; Actions → Restart instance | Shared stop/start operation for the selected service. |
| `service install --config PATH --name NAME` | Actions → Install / rebuild service | Shared `ServiceManager.install`. Options are reviewed before installation. |
| `service install --system --run-as ACCOUNT` | Open system scope, then Install / rebuild service | Uses the selected service account. Root authorization is still required for system changes. |
| `service install --enable --now --linger --replace` | Install / rebuild service form | Options and final confirmation are explicit. As on the CLI, install with Start now also enables automatic startup; plain Start does not. |
| `instance uninstall NAME`; `service uninstall` | Actions → Uninstall service (keep configuration) | Shared uninstall operation. Unit removal stops the service; configuration and workspace are retained. |
| `instance delete NAME [--yes] [--config PATH] [--system]` | Uppercase `D` / Delete; Actions → Delete instance (remove configuration) | Shared deletion backend, default-No confirmation. Stops, disables and unregisters only the selected service, removes its configuration and runtime state, and refreshes the user overview (or closes an explicit system panel). Workspace files are retained. |
| `service enable [--now]`; `service disable [--now]` | Actions → Enable/Disable automatic startup | Boot policy and immediate start/stop are separate choices. |
| `service linger` | Actions → Enable user linger | Applies to the user account rather than one instance; explicit confirmation is required. |
| `service status` | Detail page; Actions → Service details | Shows service scope, PID, state, boot setting and selected configuration. |
| `service logs --lines N [--follow]` | Actions → View / follow logs | Line count and live following are selectable. Closing the log view does not stop the service. |
| `instance link NAME [--output PATH]`; `link --config PATH [--output PATH]` | `u`; Actions → Export MCP link | `operations.export_text` and `write_export`; complete token-bearing link can be saved directly. |
| `instance client-config NAME [--output PATH]`; `client-config --config PATH` | Actions → Export HTTP client JSON | Same serializer as CLI output. Private exports are created exclusively without overwriting files. |
| `client-config --transport stdio` | Actions → Export stdio client JSON | Exports explicit stdio launch arguments for the selected workspace. |
| `daemon --config PATH`; foreground HTTP mode | Actions → Run foreground session | Runs the same daemon as an owned child, keeps its output inside curses, and stops that child with q before returning. |
| `update --check`; `update [--version VERSION] [--name NAME]` | Actions → Software updates | Check, choose a stable release, select the associated user service, install, cancel and inspect progress inside the manager. The same transactional updater verifies and replaces the shared installation. |
| `update --recover` | Software updates → Recover interrupted update | Uses the validated retained worker to restore an interrupted transaction or finish cleanup; successful install/recovery reloads the manager. |
| `--help`; `--version` | Help, service details and software update information | CLI help also remains available for automation-specific syntax. |

The `service` command's `--config`, `--name`, and `--system` selectors
correspond to the explicit configuration dialog and its user/system scope.
The `instance` catalog contains user instances; system services are opened
explicitly. System management requires root. An ordinary user can request
administrator authorization from the explicit configuration flow; a separate
confirmation defaults to No. Accepting it invokes the operating system's sudo
authentication and opens the system panel. Linux permission checks still apply.
Linger offers the same explicit authorization path if the first request is
denied. Neither opening the menu nor declining authorization invokes sudo.

## Configuration and lifecycle rules

Creating an instance produces its own private configuration and access token.
The name is optional in the native form; leaving it blank derives a name from
the chosen workspace. Review presents the destination before saving. A port,
workspace, or origin conflict must preserve accepted values and leave the form
available for editing. The `advanced` review option exposes a custom
cloudflared executable and access outside the workspace.

Before configuration changes, the program identifies the selected service and
asks to pause it. Confirming pauses only that service. Saving or cancelling does
not restart it; Start is a separate action. Declining the pause leaves the
service running and its configuration unchanged.

The service-only CLI preserves existing unit-name selection: when `--config`
is omitted, it uses the configuration recorded by the installed managed unit.
An explicit `--config` must match that binding. A service-only Start requires an
installed unit; instance/TUI Start already knows a saved instance configuration
and can register it. This prevents an unknown service name from silently using
the default configuration.

Start, automatic startup and user linger are separate operations. Start makes
the selected instance run now. Automatic startup sets its systemd enablement.
User linger affects the login account's user service manager. Merely creating,
opening or starting another instance must not change the latter two settings.

Service uninstall removes the generated unit and stops that instance. It keeps
the saved configuration, access token and workspace files, so a later Start can
register the same instance again.

Deleting an instance is a separate operation. The confirmation identifies its
name and configuration path and explains what will be removed. No is the default;
cancelling makes no service or configuration change. After confirmation, the
selected managed service is stopped, disabled and unregistered before its
configuration and runtime state are removed. A service-removal failure keeps the
configuration available. Deletion refreshes the user overview, including when the last
instance is removed. Deleting from an explicit system panel closes that panel
without opening root's user catalog. A saved instance that was never started can also be deleted.
Workspace directories and files, other instances, account linger, and remote
Cloudflare Tunnel/DNS resources are retained. Removing the saved configuration
releases its name, workspace and port reservations; a new instance must still pass
normal conflict and available-port checks.

The deletion backend verifies the selected configuration and service binding,
refuses discovered shared or ambiguous configurations and changed files, and does not kill an
unverified foreground process. Explicit paths and system scope use the same
operation as named instances; system changes require the existing administrator
authorization. The CLI requires explicit `--yes` in noninteractive use and does
not read piped protocol data as confirmation.

Shared-binding discovery covers the current catalog, the service owner's default
and configuration-derived catalog locations, the custom XDG location used by a
live user service manager, and generated user/system units in those locations.
It also rejects configurations whose derived startup-record paths collide.
This is not a global inventory of arbitrary unregistered or inactive external
catalogs. User deletion holds the normal catalog lock; system deletion uses file
snapshots and revalidation without creating or locking a root catalog.

Link and client JSON exports contain connection credentials. The dedicated
views intentionally display them, and exported files are private. Ordinary
metadata listings, status messages, form errors and log views redact saved
tokens. Existing output files are not overwritten.

## Terminal behavior

New-instance and configuration forms stay inside curses; they do not launch
an external shell or ask the user to run a printed command. The same applies
to service controls, export dialogs, log views, foreground sessions and update
progress. Foreground sessions and the updater run as child processes while the
interface keeps control of the terminal. A successful software replacement
restores curses before reloading the updated manager. Explicit administrator
authentication temporarily returns control to the operating system's terminal
prompt and restores the panel afterwards.

Long menus scroll with Up/Down or j/k, PgUp/PgDn, Home and End. Enter chooses the
highlighted action and Esc/q returns. Form input supports cursor movement,
Backspace/Delete, Ctrl+U, and Ctrl+W. Shrinking a form below its supported size
shows a resize hint and still permits cancellation.

## Protocol launch options

The foreground CLI also provides `--transport stdio|http`, `--workspace`,
`--port`, `--token-env`, `--public-origin`, and `--allow-outside-workspace`.
The HTTP foreground view uses the selected saved configuration, including its
workspace, port, credentials, origin and access policy. `--token-env` selects an
environment variable for a direct HTTP launch; managed instances store their
own private token instead. A stdio session is launched by an MCP client using
the exported stdio JSON, because the client needs exclusive ownership of the
protocol's input and output streams. The explicit stdio export includes the
workspace and outside-workspace policy without requiring a service.

## Verification record

The parity integration suite is `tests/integration/test_tui_parity.py`.
It uses a real PTY and actual CLI subprocesses. Its visible-screen assertions
interpret cursor/erase updates, so text from an earlier screen cannot satisfy
a later-stage assertion. Captured transcripts are never included in failures.

All eight parity cases passed on an ordinary WSL Ubuntu account with its real
user systemd manager (2026-10-28):

- Four 64 × 18 creation routes: overview, detail, bare command and explicit
  configuration. Tests check native creation, default No at Start, detail-to-list
  navigation and cancellation.
- Named-mode conflict retry: a reserved port is refused visibly; changing only
  the port preserves workspace/mode/origin/token, and secret input is not echoed.
- MCP link, HTTP JSON and stdio JSON native exports match CLI output exactly,
  have mode 0600, and refuse to overwrite an existing file.
- An explicit configuration is created inside curses. Opening system scope
  presents the authorization choice; declining leaves configuration unchanged
  and creates no unit.
- Two independent live instances exercise CLI creation and TUI creation,
  Start without implicit enable/linger, cancelled boot-setting changes with no
  effect, enable without immediate state change, disable with immediate stop,
  CLI restart, configuration pause/cancel, native
  logs with line-count/follow controls, and TUI/CLI uninstall with retained
  configuration. The other instance retains its PID and continues responding
  to actual MCP reads throughout. The retained configuration starts again.

Run the ordinary terminal checks with:

```sh
python -m pytest tests/integration/test_tui_parity.py -q
```

Run the complete suite from an ordinary Linux session with user systemd:

```sh
KT_MCP_TEST_SYSTEMD=1 python -m pytest tests/integration/test_tui_parity.py -q
```

The real-terminal suite deliberately declines system authorization and never
changes account linger. It does not perform an installed software replacement.
Those operations require separate permission/backend tests and the package
update/recovery verification gates; a passing parity run alone is not evidence
of a successful release or upgrade.

The ordinary terminal tests use an isolated `XDG_CONFIG_HOME` and do not
register any services. Real-systemd tests require
`KT_MCP_TEST_SYSTEMD=1` and an ordinary Linux user. They reserve random
`kt-mcp-pty-*` names, verify each unit's executable and config binding before
cleanup, and retain a configuration if safe cleanup cannot be established.
The existing default `kt-mcp.service` is never controlled; its file hashes,
permissions and selected status fields are compared before and after.
