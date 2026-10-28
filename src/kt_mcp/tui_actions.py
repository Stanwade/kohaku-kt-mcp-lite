"""Complete in-application management menus, using the command-line backends."""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path

from kt_mcp import instances, operations
from kt_mcp.daemon import default_config_path
from kt_mcp.onboarding import SetupCancelled, _collect_config, setup_config
from kt_mcp.service import ServiceManager
from kt_mcp.tui_forms import choose, confirm, message, run_wizard

GLOBAL_ACTIONS = [
    ("new", "New instance"),
    ("instances", "All instances"),
    ("open", "Open / create configuration"),
    ("updates", "Software updates"),
    ("catalog", "Export instance list"),
]
INSTANCE_ACTIONS = [
    ("start", "Start instance"),
    ("stop", "Stop instance"),
    ("restart", "Restart instance"),
    ("configure", "Configure instance"),
    ("install", "Install / rebuild service"),
    ("uninstall", "Uninstall service (keep configuration)"),
    ("enable", "Enable automatic startup"),
    ("disable", "Disable automatic startup"),
    ("linger", "Enable user linger"),
    ("logs", "View / follow logs"),
    ("link", "Export MCP link"),
    ("client-config", "Export HTTP client JSON"),
    ("stdio", "Export stdio client JSON"),
    ("foreground", "Run foreground session"),
    ("details", "Service details"),
]


def _answer(ask, prompt, default=""):
    value = ask(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    if value.lower() == ":q":
        raise SetupCancelled("Operation cancelled.")
    return value or default


def _yes(ask, prompt, default=False):
    while True:
        value = _answer(ask, prompt, "yes" if default else "no").lower()
        if value in {"yes", "y", "1"}:
            return True
        if value in {"no", "n", "0"}:
            return False
        print("Enter yes or no.")


def _save_dialog(screen, title, text, default):
    def collect(*, ask, ask_secret):
        while True:
            value = _answer(
                ask, "Export file (a new file; existing files are preserved)", default
            )
            try:
                return operations.write_export(Path(value), text)
            except (OSError, ValueError) as error:
                print(f"Export not saved: {error}")

    saved = run_wizard(screen, title, collect)
    message(
        screen,
        "Export saved",
        str(saved) + "\nThe file contains the complete unwrapped text.",
    )
    return saved


def show_export(screen, panel, kind="link"):
    import curses

    from kt_mcp import tui

    panel.refresh()
    text = operations.export_text(panel.config_path, kind)
    title = {
        "link": "MCP link",
        "client-config": "HTTP client configuration",
        "stdio": "Stdio client configuration",
    }[kind]
    suffix = ".txt" if kind == "link" else ".json"
    offset = 0
    screen.timeout(250)
    try:
        while True:
            height, width = screen.getmaxyx()
            screen.erase()
            tui._put(screen, 0, 1, title, curses.A_BOLD)
            page = max(1, height - 5)
            lines = tui.wrap_display(text, max(2, width - 4))
            offset = max(0, min(offset, max(0, len(lines) - page)))
            for row, line in enumerate(lines[offset : offset + page], 2):
                tui._put(screen, row, 2, line)
            tui._put(
                screen,
                height - 2,
                1,
                "Contains client access information. Export only when needed.",
            )
            tui._put(
                screen,
                height - 1,
                1,
                "[w] Save file  Up/Down/PgUp/PgDn: scroll  [q] Back",
            )
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q"), 27, 10, 13):
                return
            if key in (ord("w"), ord("W")):
                try:
                    _save_dialog(
                        screen,
                        title,
                        text,
                        str(Path.home() / (panel.manager.name + "-" + kind + suffix)),
                    )
                except SetupCancelled:
                    pass
            elif key in (curses.KEY_DOWN, ord("j")):
                offset += 1
            elif key in (curses.KEY_UP, ord("k")):
                offset -= 1
            elif key == curses.KEY_NPAGE:
                offset += page
            elif key == curses.KEY_PPAGE:
                offset -= page
    finally:
        screen.timeout(250)
        screen.clearok(True)


def show_logs(screen, panel):
    import curses

    from kt_mcp import tui

    count = 200
    following = True
    offset = 0
    next_refresh = 0.0
    text = ""
    screen.timeout(250)
    try:
        while True:
            if time.monotonic() >= next_refresh:
                try:
                    text = panel.redact(panel.manager.logs(count))
                except tui.UI_ERRORS as error:
                    text = panel.redact(str(error))
                next_refresh = time.monotonic() + (2 if following else 86400)
            height, width = screen.getmaxyx()
            lines = tui.wrap_display(text or "No log entries.", max(2, width - 4))
            page = max(1, height - 4)
            if following:
                offset = max(0, len(lines) - page)
            offset = max(0, min(offset, max(0, len(lines) - page)))
            screen.erase()
            tui._put(
                screen,
                0,
                1,
                f"Service logs - {'Following' if following else 'Paused'} - {count} lines",
                curses.A_BOLD,
            )
            for row, line in enumerate(lines[offset : offset + page], 2):
                tui._put(screen, row, 2, line)
            tui._put(
                screen, height - 1, 1, "[f] Follow/pause [n] Lines [w] Save [q] Back"
            )
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q"), 27):
                return
            if key in (ord("f"), ord("F")):
                following = not following
                next_refresh = 0
            elif key in (ord("n"), ord("N")):

                def collect(*, ask, ask_secret, current=count):
                    while True:
                        value = _answer(ask, "Log lines (1-1000)", str(current))
                        try:
                            number = int(value)
                            if 1 <= number <= 1000:
                                return number
                        except ValueError:
                            pass
                        print("Enter an integer from 1 to 1000.")

                try:
                    count = run_wizard(screen, "Log history", collect)
                    next_refresh = 0
                except SetupCancelled:
                    pass
            elif key in (ord("w"), ord("W")):
                try:
                    _save_dialog(
                        screen,
                        "Export redacted logs",
                        text,
                        str(Path.home() / (panel.manager.name + ".log")),
                    )
                except SetupCancelled:
                    pass
            elif key in (
                curses.KEY_DOWN,
                curses.KEY_UP,
                curses.KEY_NPAGE,
                curses.KEY_PPAGE,
            ):
                following = False
                next_refresh = time.monotonic() + 86400
                offset += {
                    curses.KEY_DOWN: 1,
                    curses.KEY_UP: -1,
                    curses.KEY_NPAGE: page,
                    curses.KEY_PPAGE: -page,
                }[key]
    finally:
        screen.timeout(250)
        screen.clearok(True)


def install_dialog(screen, panel):
    def collect(*, ask, ask_secret):
        print(
            "Register this configuration with systemd. Choose each startup option explicitly."
        )
        run_as = (
            _answer(ask, "Run as ordinary user", os.environ.get("SUDO_USER", ""))
            if panel.manager.system
            else None
        )
        return {
            "run_as": run_as,
            "now": _yes(ask, "Start now?"),
            "enable": _yes(ask, "Enable automatic startup?"),
            "linger": (
                _yes(ask, "Enable user linger?") if not panel.manager.system else False
            ),
            "replace": _yes(ask, "Replace existing generated unit?"),
        }

    options = run_wizard(screen, "Install service", collect)
    summary = (
        f"Service: {panel.manager.unit_name}\n"
        f"Start now: {options['now']}\nAutomatic startup: {options['enable'] or options['now']}\n"
        f"User linger: {options['linger']}\nReplace: {options['replace']}"
    )
    if options["now"]:
        summary += "\nService install --now also enables automatic startup."
    summary += f"\nConfiguration: {panel.config_path}"
    if not confirm(screen, "Install service", summary):
        return
    panel.manager.install(panel.config_path, **options)
    if options["now"]:
        operations.control_service(panel.manager, panel.config_path, "start")
    panel.note = "Service registered with the selected startup options."
    panel.refresh()


def _run_authentication(
    screen, arguments, *, environment=None, user=None, group=None, extra_groups=None
):
    """The system owns password entry; the application resumes after authentication."""
    import curses

    curses.def_prog_mode()
    curses.endwin()
    try:
        kwargs = {}
        if user is not None:
            kwargs.update(user=user, group=group, extra_groups=extra_groups or [])
        result = subprocess.run(arguments, env=environment, check=False, **kwargs)
        return result.returncode
    except KeyboardInterrupt:
        return 130
    finally:
        curses.reset_prog_mode()
        screen.keypad(True)
        screen.timeout(250)
        screen.clearok(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def _admin_panel(screen, path, name):
    if not confirm(
        screen,
        "Administrator authorization",
        "Authenticate with the operating system to manage this system service.\n"
        f"Service: {name}\nConfiguration: {path}",
    ):
        return
    code = _run_authentication(
        screen,
        [
            "sudo",
            "--",
            os.path.abspath(sys.executable),
            "-I",
            "-m",
            "kt_mcp",
            "tui",
            "--system",
            "--config",
            str(path),
            "--name",
            name,
        ],
    )
    if code:
        message(
            screen,
            "System panel closed",
            f"Administrator session returned code {code}. No authorization was bypassed.",
        )


def _user_panel(screen, path, name, account_name):
    import pwd

    try:
        account = pwd.getpwnam(account_name)
    except KeyError:
        raise ValueError("The selected user account does not exist.") from None
    if account.pw_uid == 0:
        raise ValueError("Choose an ordinary user account.")
    environment = dict(os.environ)
    environment.update(
        HOME=account.pw_dir,
        USER=account.pw_name,
        LOGNAME=account.pw_name,
        XDG_RUNTIME_DIR=f"/run/user/{account.pw_uid}",
        DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{account.pw_uid}/bus",
    )
    environment.pop("XDG_CONFIG_HOME", None)
    environment.pop("XDG_DATA_HOME", None)
    environment.pop("KT_MCP_TOKEN", None)
    environment.pop("TUNNEL_TOKEN", None)
    _run_authentication(
        screen,
        [
            os.path.abspath(sys.executable),
            "-I",
            "-m",
            "kt_mcp",
            "tui",
            "--config",
            str(path),
            "--name",
            name,
        ],
        environment=environment,
        user=account.pw_uid,
        group=account.pw_gid,
        extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
    )


def prepare_system_config(screen, path):
    """Create a system daemon configuration as its explicitly chosen ordinary user."""
    import pwd

    from kt_mcp.desktop import DesktopSettings

    if os.geteuid() != 0:
        message(
            screen,
            "Administrator authorization required",
            "Use Actions > Open / create configuration > system to authenticate.",
        )
        return False

    def collect(*, ask, ask_secret):
        while True:
            name = _answer(ask, "Run as ordinary user", os.environ.get("SUDO_USER", ""))
            try:
                account = pwd.getpwnam(name)
                if account.pw_uid != 0:
                    break
            except KeyError:
                pass
            print("Choose an existing ordinary user account.")
        print(f"The configuration will be written by {account.pw_name}.")

        def save(config):
            payload = {
                **asdict(config.settings),
                "token": config.token,
                "tunnel_token": config.tunnel_token,
            }
            script = (
                "import json,sys; from pathlib import Path; "
                "from kt_mcp.daemon import DaemonConfig,save_config; "
                "from kt_mcp.desktop import DesktopSettings; "
                "d=json.load(sys.stdin); t=d.pop('token'); u=d.pop('tunnel_token'); "
                "save_config(DaemonConfig(DesktopSettings(**d),t,u),Path(sys.argv[1]))"
            )
            environment = dict(os.environ)
            environment["HOME"] = account.pw_dir
            environment.pop("KT_MCP_TOKEN", None)
            environment.pop("TUNNEL_TOKEN", None)
            process = subprocess.run(
                [os.path.abspath(sys.executable), "-I", "-c", script, str(path)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                user=account.pw_uid,
                group=account.pw_gid,
                extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
                env=environment,
            )
            if process.returncode:
                raise ValueError(
                    "The selected account could not create this configuration. "
                    "Choose a path that account can write and an installation it can read."
                )

        import secrets

        _collect_config(
            path,
            DesktopSettings(workspace=account.pw_dir, mode="local"),
            secrets.token_urlsafe(32),
            ask=ask,
            ask_secret=ask_secret,
            tunnel_token="",
            environment_tunnel_token="",
            save=save,
            title="System service configuration",
            retry_save=True,
        )
        return True

    try:
        return run_wizard(screen, "Create system configuration", collect)
    except SetupCancelled:
        return False


def open_configuration(screen):
    from kt_mcp import tui

    def collect(*, ask, ask_secret):
        path = (
            Path(_answer(ask, "Configuration file", str(default_config_path())))
            .expanduser()
            .absolute()
        )
        name = instances.canonical_name(_answer(ask, "Service name", "kt-mcp"))
        scope = _answer(ask, "Service scope (user/system)", "user").lower()
        if scope not in {"user", "system"}:
            raise ValueError("Service scope must be user or system.")
        account = ""
        if os.geteuid() == 0 and scope == "user":
            account = _answer(ask, "User account", os.environ.get("SUDO_USER", ""))
        return path, name, scope == "system", account

    path, name, system, account = run_wizard(screen, "Open configuration", collect)
    if system and os.geteuid() != 0:
        _admin_panel(screen, path, name)
        return None
    if not system and os.geteuid() == 0:
        _user_panel(screen, path, name, account)
        return None
    if not path.exists():
        if system:
            if not prepare_system_config(screen, path):
                return None
        else:
            run_wizard(
                screen,
                "Create configuration",
                lambda **readers: setup_config(path, **readers),
            )
    return tui.TerminalController(ServiceManager(name, system), path)


def _enable_linger(screen, panel):
    if panel.manager.system:
        raise ValueError("System services do not use user linger.")
    if not confirm(
        screen,
        "Enable user linger",
        "Linger is an account-wide setting. It allows user services to continue after logout.",
    ):
        return
    try:
        panel.manager.enable_linger()
    except (PermissionError, OSError, RuntimeError) as error:
        import pwd

        from kt_mcp.service import ServiceError

        if not isinstance(error, (PermissionError, ServiceError)):
            raise
        if not confirm(
            screen,
            "Administrator authorization",
            "Linger was not enabled. Authenticate with the operating system to retry?",
        ):
            return
        account = pwd.getpwuid(os.geteuid()).pw_name
        code = _run_authentication(
            screen, ["sudo", "--", "loginctl", "enable-linger", account]
        )
        if code:
            raise ValueError(f"Linger authorization failed (exit {code}).")
    panel.note = "User linger enabled."
    panel.refresh()


def run_foreground(screen, panel):
    """Run the existing daemon backend as an owned child, with an in-app Stop action."""
    import curses

    from kt_mcp import tui

    panel.refresh()
    if panel.status.get("ActiveState") in {
        "active",
        "activating",
        "reloading",
        "deactivating",
    }:
        raise ValueError(
            "Stop this systemd service in the manager before starting a foreground session."
        )
    panel._check_manual_daemon()
    if not panel.manager.system:
        instances.ensure_available(
            panel.config, exclude=panel.config_path, check_socket=True
        )
    if not confirm(
        screen,
        "Foreground session",
        "Run this instance while this view is open?\nPress q to stop this session and return.",
    ):
        return
    environment = dict(os.environ, PYTHONUNBUFFERED="1")
    environment.pop("KT_MCP_TOKEN", None)
    environment.pop("TUNNEL_TOKEN", None)
    identity = {}
    if panel.manager.system:
        if os.geteuid() != 0:
            raise PermissionError(
                "Authenticate for system management in Actions first."
            )
        import pwd

        account = pwd.getpwuid(panel.config_path.stat().st_uid)
        if account.pw_uid == 0:
            raise ValueError("The configuration must belong to an ordinary run user.")
        identity = {
            "user": account.pw_uid,
            "group": account.pw_gid,
            "extra_groups": os.getgrouplist(account.pw_name, account.pw_gid),
        }
        environment["HOME"] = account.pw_dir
    process = subprocess.Popen(
        [
            os.path.abspath(sys.executable),
            "-I",
            "-m",
            "kt_mcp",
            "daemon",
            "--config",
            str(panel.config_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
        env=environment,
        **identity,
    )
    output = queue.Queue(maxsize=500)
    lines = deque(maxlen=300)

    def read_output():
        for line in process.stdout:
            try:
                output.put_nowait(panel.redact(line.rstrip("\n")))
            except queue.Full:
                pass

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    stopping = False
    stop_deadline = None
    screen.timeout(250)
    try:
        while True:
            while True:
                try:
                    lines.append(output.get_nowait())
                except queue.Empty:
                    break
            height, width = screen.getmaxyx()
            screen.erase()
            tui._put(
                screen,
                0,
                1,
                "Foreground session" + (" - stopping" if stopping else ""),
                curses.A_BOLD,
            )
            wrapped = []
            for line in lines:
                wrapped.extend(tui.wrap_display(line, max(2, width - 4)))
            for row, line in enumerate(wrapped[-max(1, height - 4) :], 2):
                tui._put(screen, row, 2, line)
            done = process.poll() is not None
            tui._put(
                screen,
                height - 1,
                1,
                "[q] Return" if done else "[q] Stop session and return",
            )
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q"), 27, 3):
                if done:
                    return
                if not stopping:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    stopping = True
                    stop_deadline = time.monotonic() + 30
            if stopping and not done and time.monotonic() >= stop_deadline:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                panel.note = (
                    "Foreground session exceeded its stop timeout and was terminated."
                )
                return
            if stopping and done:
                return
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        process.stdout.close()
        reader.join(timeout=1)
        screen.timeout(250)
        screen.clearok(True)


def actions_menu(screen, panel=None):
    from kt_mcp import tui

    options = [
        (key, label)
        for key, label in GLOBAL_ACTIONS
        if os.geteuid() != 0 or key not in {"new", "instances", "catalog"}
    ]
    if panel is not None:
        options.extend(
            (key, label)
            for key, label in INSTANCE_ACTIONS
            if key != "linger" or not panel.manager.system
        )
        if panel.manager.system and os.geteuid() != 0:
            options.append(("admin", "Authenticate for system management"))
    choice = choose(
        screen,
        "Actions",
        options,
        body=(
            ("Selected: " + panel.manager.name)
            if panel is not None
            else "Global management"
        ),
    )
    if choice is None:
        return None
    if choice in {"new", "instances"}:
        return choice
    try:
        if choice == "open":
            return open_configuration(screen)
        if choice == "updates":
            from kt_mcp.tui_update import run_update

            run_update(screen, panel)
        elif choice == "catalog":
            _save_dialog(
                screen,
                "Export instance list",
                json.dumps(
                    operations.instance_metadata(), ensure_ascii=False, indent=2
                ),
                str(Path.home() / "kt-mcp-instances.json"),
            )
        elif choice in {"start", "stop", "restart"}:
            panel.status = operations.control_service(
                panel.manager, panel.config_path, choice
            )
            panel.note = f"{choice}: completed."
        elif choice == "configure":
            tui._configure(screen, panel)
        elif choice == "install":
            install_dialog(screen, panel)
        elif choice == "uninstall":
            if confirm(
                screen,
                "Uninstall service",
                f"Stop and unregister {panel.manager.name}?\n"
                "The saved configuration and workspace files will be retained.",
            ):
                operations.uninstall_service(panel.manager, panel.config_path)
                panel.note = (
                    "Service uninstalled; configuration and workspace retained."
                )
        elif choice in {"enable", "disable"}:
            effect = choose(
                screen,
                "Apply running state too?",
                [
                    ("only", "Only change automatic startup"),
                    (
                        "now",
                        "Also start now" if choice == "enable" else "Also stop now",
                    ),
                ],
                body=f"{choice.title()} automatic startup for {panel.manager.name}. Esc cancels.",
            )
            if effect is None:
                return None
            panel.status = operations.service_setting(
                panel.manager, panel.config_path, choice, now=effect == "now"
            )
            panel.note = f"Automatic startup {choice}d."
        elif choice == "linger":
            _enable_linger(screen, panel)
        elif choice == "logs":
            show_logs(screen, panel)
        elif choice in {"link", "client-config", "stdio"}:
            show_export(screen, panel, choice)
        elif choice == "foreground":
            run_foreground(screen, panel)
        elif choice == "details":
            panel.refresh()
            message(screen, "Service details", panel.details_text())
        elif choice == "admin":
            _admin_panel(screen, panel.config_path, panel.manager.name)
        if panel is not None:
            panel.refresh()
    except SetupCancelled:
        if panel is not None:
            panel.note = "Operation cancelled."
    except tui.UI_ERRORS as error:
        text = panel.redact(str(error)) if panel is not None else str(error)
        message(screen, "Action could not be completed", text)
    return None
