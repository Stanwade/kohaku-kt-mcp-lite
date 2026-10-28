import io
import json
import os
import signal
import sys
from types import SimpleNamespace

import pytest

from kt_mcp import tui
from kt_mcp import tui_actions as actions
from kt_mcp.daemon import DaemonConfig
from kt_mcp.desktop import DesktopSettings
from kt_mcp.onboarding import SetupCancelled

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux terminal actions"
)


class Screen:
    def __init__(self, keys=(), *, height=18, width=64):
        self.keys = iter(keys)
        self.height = height
        self.width = width
        self.frames = []
        self.drawn = []
        self.timeouts = []
        self.clears = []

    def getmaxyx(self):
        return self.height, self.width

    def keypad(self, value):
        pass

    def timeout(self, value):
        self.timeouts.append(value)

    def clearok(self, value):
        self.clears.append(value)

    def erase(self):
        self.drawn = []

    def addnstr(self, row, column, text, limit, attribute=0):
        value = text[:limit]
        assert 0 <= row < self.height
        assert column + tui.display_width(value) < self.width
        self.drawn.append((row, value))

    def refresh(self):
        self.frames.append(list(self.drawn))

    def getch(self):
        value = next(self.keys)
        return ord(value) if isinstance(value, str) else value

    def rendered(self):
        return "\n".join(text for frame in self.frames for _, text in frame)


class Manager:
    def __init__(self):
        self.name = "kt-mcp-selected"
        self.unit_name = self.name + ".service"
        self.system = False
        self.calls = []
        self.log_text = ""
        self.linger_error = None

    def logs(self, count):
        self.calls.append(("logs", count))
        return self.log_text

    def install(self, path, **options):
        self.calls.append(("install", path, options))

    def enable_linger(self):
        self.calls.append(("linger",))
        if self.linger_error:
            raise self.linger_error


class Panel:
    token = "synthetic-client-token"
    tunnel_token = "synthetic-tunnel-token"

    def __init__(self, path):
        self.config_path = path
        self.manager = Manager()
        self.config = DaemonConfig(
            DesktopSettings(workspace=str(path.parent)), self.token, self.tunnel_token
        )
        self.status = {"ActiveState": "inactive", "MainPID": "0"}
        self.note = ""
        self.refreshes = 0
        self.manual_checks = 0

    def refresh(self):
        self.refreshes += 1

    def redact(self, text):
        return (
            str(text)
            .replace(self.token, "[redacted]")
            .replace(self.tunnel_token, "[redacted]")
        )

    def _check_manual_daemon(self):
        self.manual_checks += 1


@pytest.fixture(autouse=True)
def no_external_side_effects(monkeypatch):
    import curses

    def forbidden(*args, **kwargs):
        pytest.fail(
            "This UI test must not run a process, signal, service or catalog operation"
        )

    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    monkeypatch.setattr(actions.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(actions.subprocess, "run", forbidden)
    monkeypatch.setattr(actions.subprocess, "Popen", forbidden)
    monkeypatch.setattr(actions.os, "killpg", forbidden)
    monkeypatch.setattr(actions.instances, "ensure_available", forbidden)
    monkeypatch.setattr(actions.operations, "control_service", forbidden)
    monkeypatch.setattr(actions.operations, "service_setting", forbidden)
    monkeypatch.setattr(actions.operations, "uninstall_service", forbidden)
    monkeypatch.setattr(actions.operations, "instance_metadata", forbidden)
    authentication = actions._run_authentication
    monkeypatch.setattr(actions, "_run_authentication", forbidden)
    return authentication


@pytest.fixture
def panel(tmp_path):
    return Panel(tmp_path / "selected.json")


@pytest.fixture
def messages(monkeypatch):
    captured = []
    monkeypatch.setattr(
        actions, "message", lambda screen, title, body: captured.append((title, body))
    )
    return captured


def wizard_answers(monkeypatch, values):
    answers = iter(values)

    def run(screen, title, callback):
        return callback(
            ask=lambda prompt: next(answers), ask_secret=lambda prompt: next(answers)
        )

    monkeypatch.setattr(actions, "run_wizard", run)


def menu_answers(monkeypatch, values):
    answers = iter(values)
    monkeypatch.setattr(actions, "choose", lambda *args, **kwargs: next(answers))


def test_empty_overview_actions_offer_creation_without_instance_actions(monkeypatch):
    captured = []

    def choose(screen, title, options, **kwargs):
        captured.extend(options)
        return "new"

    monkeypatch.setattr(actions, "choose", choose)
    assert actions.actions_menu(Screen()) == "new"
    ids = {key for key, _ in captured}
    assert {"new", "instances", "open", "updates", "catalog"} <= ids
    assert not {"start", "stop", "uninstall", "enable"} & ids


def test_root_system_actions_cannot_enter_the_root_user_catalog(monkeypatch, panel):
    monkeypatch.setattr(actions.os, "geteuid", lambda: 0)
    panel.manager.system = True
    captured = []

    def choose(screen, title, options, **kwargs):
        captured.extend(options)

    monkeypatch.setattr(actions, "choose", choose)
    assert actions.actions_menu(Screen(), panel) is None
    ids = {key for key, _ in captured}
    assert not {"new", "instances", "catalog", "linger"} & ids
    assert {"open", "updates", "configure", "install"} <= ids


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_actions_control_only_the_selected_configuration(monkeypatch, panel, action):
    menu_answers(monkeypatch, [action])
    calls = []

    def control(manager, path, selected_action, **options):
        calls.append((manager, path, selected_action))
        return {"ActiveState": "active", "MainPID": "123"}

    monkeypatch.setattr(actions.operations, "control_service", control)
    actions.actions_menu(Screen(), panel)
    assert calls == [(panel.manager, panel.config_path, action)]
    assert panel.status["MainPID"] == "123"
    assert panel.refreshes == 1


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_cancelling_startup_scope_does_not_change_any_service_setting(
    monkeypatch, panel, action
):
    menu_answers(monkeypatch, [action, None])
    actions.actions_menu(Screen(), panel)
    assert panel.note == ""
    assert panel.manager.calls == []


@pytest.mark.parametrize(
    ("action", "effect", "now"),
    [
        ("enable", "only", False),
        ("enable", "now", True),
        ("disable", "only", False),
        ("disable", "now", True),
    ],
)
def test_startup_scope_is_explicit_and_bound_to_selected_configuration(
    monkeypatch, panel, action, effect, now
):
    menu_answers(monkeypatch, [action, effect])
    calls = []

    def setting(manager, path, selected_action, **kwargs):
        calls.append((manager, path, selected_action, kwargs))
        return {"UnitFileState": "enabled" if action == "enable" else "disabled"}

    monkeypatch.setattr(actions.operations, "service_setting", setting)
    actions.actions_menu(Screen(), panel)
    assert calls == [(panel.manager, panel.config_path, action, {"now": now})]


def test_action_errors_return_to_panel_without_showing_credentials(
    monkeypatch, panel, messages
):
    menu_answers(monkeypatch, ["start"])

    def failure(*args, **kwargs):
        raise ValueError("Endpoint failed: " + panel.token + " " + panel.tunnel_token)

    monkeypatch.setattr(actions.operations, "control_service", failure)
    assert actions.actions_menu(Screen(), panel) is None
    assert len(messages) == 1
    assert panel.token not in messages[0][1]
    assert panel.tunnel_token not in messages[0][1]
    assert "[redacted]" in messages[0][1]


def test_uninstall_decline_leaves_the_selected_unit_untouched(monkeypatch, panel):
    menu_answers(monkeypatch, ["uninstall"])
    bodies = []

    def decline(screen, title, body):
        bodies.append(body)
        return False

    monkeypatch.setattr(actions, "confirm", decline)
    actions.actions_menu(Screen(), panel)
    assert panel.manager.calls == []
    assert "configuration and workspace files will be retained" in bodies[0]


def test_install_decline_cannot_register_or_enable_anything(monkeypatch, panel):
    wizard_answers(monkeypatch, ["yes", "yes", "yes", "yes"])
    summaries = []

    def decline(screen, title, body):
        summaries.append(body)
        return False

    monkeypatch.setattr(actions, "confirm", decline)
    actions.install_dialog(Screen(), panel)
    assert panel.manager.calls == []
    summary = summaries[0]
    for impact in ("Automatic startup: True", "User linger: True", "Replace: True"):
        assert summary.index(impact) < summary.index("Configuration:")
    assert "--now also enables automatic startup" in summary


def test_install_defaults_do_not_enable_boot_or_linger(monkeypatch, panel):
    wizard_answers(monkeypatch, ["", "", "", ""])
    monkeypatch.setattr(actions, "confirm", lambda *args, **kwargs: True)
    actions.install_dialog(Screen(), panel)
    assert panel.manager.calls == [
        (
            "install",
            panel.config_path,
            {
                "run_as": None,
                "now": False,
                "enable": False,
                "linger": False,
                "replace": False,
            },
        )
    ]


@pytest.mark.parametrize("accept_retry", [False, True])
def test_linger_permission_retry_requires_separate_authorization(
    monkeypatch, panel, accept_retry
):
    import pwd

    panel.manager.linger_error = PermissionError("authorization required")
    decisions = iter([True, accept_retry])
    prompts = []
    authenticated = []

    def confirm(screen, title, body):
        prompts.append((title, body))
        return next(decisions)

    monkeypatch.setattr(actions, "confirm", confirm)
    monkeypatch.setattr(
        pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="selected-user")
    )

    def authenticate(screen, arguments, **kwargs):
        authenticated.append((arguments, kwargs))
        return 0

    monkeypatch.setattr(actions, "_run_authentication", authenticate)
    actions._enable_linger(Screen(), panel)
    assert panel.manager.calls == [("linger",)]
    assert "account-wide" in prompts[0][1]
    if accept_retry:
        assert authenticated == [
            (["sudo", "--", "loginctl", "enable-linger", "selected-user"], {})
        ]
    else:
        assert authenticated == []


def test_linger_initial_decline_never_attempts_a_permission_change(monkeypatch, panel):
    monkeypatch.setattr(actions, "confirm", lambda *args, **kwargs: False)
    actions._enable_linger(Screen(), panel)
    assert panel.manager.calls == []


@pytest.mark.parametrize("approved", [False, True])
def test_system_authorization_preserves_exact_selected_binding(
    monkeypatch, panel, approved
):
    monkeypatch.setattr(actions, "confirm", lambda *args, **kwargs: approved)
    authenticated = []

    def authenticate(screen, arguments, **kwargs):
        authenticated.append((arguments, kwargs))
        return 0

    monkeypatch.setattr(actions, "_run_authentication", authenticate)
    actions._admin_panel(Screen(), panel.config_path, panel.manager.name)
    if approved:
        assert authenticated == [
            (
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
                    str(panel.config_path),
                    "--name",
                    panel.manager.name,
                ],
                {},
            )
        ]
    else:
        assert authenticated == []


def test_user_panel_switches_identity_and_user_bus_without_root_catalog_paths(
    monkeypatch, panel
):
    import pwd

    monkeypatch.setenv("KT_MCP_TOKEN", "ambient-root-client-token")
    monkeypatch.setenv("TUNNEL_TOKEN", "ambient-root-tunnel-token")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/root/private-config")
    monkeypatch.setenv("XDG_DATA_HOME", "/root/private-data")
    account = SimpleNamespace(
        pw_uid=1201, pw_gid=1202, pw_name="chosen", pw_dir="/home/chosen"
    )
    monkeypatch.setattr(pwd, "getpwnam", lambda name: account)
    monkeypatch.setattr(actions.os, "getgrouplist", lambda name, gid: [1202, 1203])
    captured = []
    monkeypatch.setattr(
        actions,
        "_run_authentication",
        lambda *args, **kwargs: captured.append((args, kwargs)) or 0,
    )

    actions._user_panel(Screen(), panel.config_path, panel.manager.name, "chosen")
    kwargs = captured[0][1]
    assert (kwargs["user"], kwargs["group"], kwargs["extra_groups"]) == (
        1201,
        1202,
        [1202, 1203],
    )
    environment = kwargs["environment"]
    assert environment["HOME"] == "/home/chosen"
    assert environment["XDG_RUNTIME_DIR"] == "/run/user/1201"
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1201/bus"
    assert "XDG_CONFIG_HOME" not in environment
    assert "XDG_DATA_HOME" not in environment
    assert "KT_MCP_TOKEN" not in environment
    assert "TUNNEL_TOKEN" not in environment


def test_system_config_creation_uses_chosen_uid_and_stdin_for_secrets(
    monkeypatch, panel
):
    import pwd

    monkeypatch.setattr(actions.os, "geteuid", lambda: 0)
    monkeypatch.setenv("KT_MCP_TOKEN", "ambient-root-client-token")
    monkeypatch.setenv("TUNNEL_TOKEN", "ambient-root-tunnel-token")
    account = SimpleNamespace(
        pw_uid=1201, pw_gid=1202, pw_name="chosen", pw_dir="/home/chosen"
    )
    monkeypatch.setattr(pwd, "getpwnam", lambda name: account)
    monkeypatch.setattr(actions.os, "getgrouplist", lambda name, gid: [1202, 1203])
    wizard_answers(monkeypatch, ["chosen"])
    calls = []

    def collect(path, defaults, token, **kwargs):
        assert kwargs["environment_tunnel_token"] == ""
        assert kwargs["retry_save"] is True
        kwargs["save"](panel.config)

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(actions, "_collect_config", collect)
    monkeypatch.setattr(actions.subprocess, "run", run)
    assert actions.prepare_system_config(Screen(), panel.config_path) is True
    arguments, kwargs = calls[0]
    assert panel.token not in repr(arguments)
    assert panel.tunnel_token not in repr(arguments)
    assert "KT_MCP_TOKEN" not in kwargs["env"]
    assert "TUNNEL_TOKEN" not in kwargs["env"]
    payload = json.loads(kwargs["input"])
    assert payload["token"] == panel.token
    assert payload["tunnel_token"] == panel.tunnel_token
    assert (kwargs["user"], kwargs["group"], kwargs["extra_groups"]) == (
        1201,
        1202,
        [1202, 1203],
    )


def test_export_save_gets_complete_unwrapped_text_and_cancel_returns_to_viewer(
    monkeypatch, panel
):
    text = "https://example.invalid/" + "p" * 250 + "?token=" + panel.token
    calls = []
    monkeypatch.setattr(actions.operations, "export_text", lambda path, kind: text)

    def save(screen, title, value, default):
        calls.append(value)
        raise SetupCancelled("cancelled")

    monkeypatch.setattr(actions, "_save_dialog", save)
    screen = Screen(["w", "q"], width=40, height=10)
    actions.show_export(screen, panel)
    assert calls == [text]
    assert len(screen.frames) == 2
    assert screen.timeouts[-1] == 250


def test_log_history_validation_and_exports_never_expose_tokens(
    monkeypatch, panel, capsys
):
    panel.manager.log_text = "request " + panel.token + "\ntunnel " + panel.tunnel_token
    wizard_answers(monkeypatch, ["0", "1001", "not-a-number", "17"])
    exported = []
    monkeypatch.setattr(
        actions,
        "_save_dialog",
        lambda screen, title, text, default: exported.append(text),
    )
    monkeypatch.setattr(actions.time, "monotonic", lambda: 1.0)
    screen = Screen(["n", "w", "q"])
    actions.show_logs(screen, panel)

    assert panel.manager.calls == [("logs", 200), ("logs", 17)]
    assert capsys.readouterr().out.count("Enter an integer from 1 to 1000.") == 3
    assert exported == ["request [redacted]\ntunnel [redacted]"]
    assert panel.token not in screen.rendered()
    assert panel.tunnel_token not in screen.rendered()


def test_log_read_errors_are_redacted_before_display_and_export(monkeypatch, panel):
    def failure(count):
        raise OSError("journal failed: " + panel.token)

    panel.manager.logs = failure
    exported = []
    monkeypatch.setattr(
        actions,
        "_save_dialog",
        lambda screen, title, text, default: exported.append(text),
    )
    screen = Screen(["w", "q"])
    actions.show_logs(screen, panel)
    assert exported == ["journal failed: [redacted]"]
    assert panel.token not in screen.rendered()


def test_foreground_stop_timeout_terminates_only_its_owned_process_group(
    monkeypatch, panel
):
    class Child:
        pid = 48291

        def __init__(self):
            self.stdout = io.StringIO("started\n")
            self.killed = False
            self.waits = []

        def poll(self):
            return -signal.SIGKILL if self.killed else None

        def wait(self, **kwargs):
            self.waits.append(kwargs)
            assert self.killed
            return -signal.SIGKILL

    child = Child()
    commands = []
    signals = []

    def launch(arguments, **kwargs):
        commands.append((arguments, kwargs))
        return child

    def killpg(pid, selected_signal):
        signals.append((pid, selected_signal))
        if selected_signal == signal.SIGKILL:
            child.killed = True

    moments = iter([10.0, 10.0, 41.0])
    monkeypatch.setattr(actions.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        actions.instances, "ensure_available", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(actions, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(actions.subprocess, "Popen", launch)
    monkeypatch.setattr(actions.os, "killpg", killpg)
    monkeypatch.setenv("KT_MCP_TOKEN", "ambient-client-token")
    monkeypatch.setenv("TUNNEL_TOKEN", "ambient-tunnel-token")
    screen = Screen(["q", -1])

    actions.run_foreground(screen, panel)
    assert signals == [(child.pid, signal.SIGTERM), (child.pid, signal.SIGKILL)]
    assert child.waits == [{"timeout": 5}]
    assert child.stdout.closed
    assert commands[0][0][-2:] == ["--config", str(panel.config_path)]
    assert commands[0][1]["start_new_session"] is True
    assert "KT_MCP_TOKEN" not in commands[0][1]["env"]
    assert "TUNNEL_TOKEN" not in commands[0][1]["env"]
    assert "stop timeout" in panel.note
    assert screen.timeouts[-1] == 250


def test_authentication_interrupt_returns_to_curses_instead_of_exiting(
    monkeypatch, no_external_side_effects
):
    import curses

    restored = []
    monkeypatch.setattr(curses, "def_prog_mode", lambda: restored.append("save"))
    monkeypatch.setattr(curses, "endwin", lambda: restored.append("handoff"))
    monkeypatch.setattr(curses, "reset_prog_mode", lambda: restored.append("resume"))

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(actions.subprocess, "run", interrupted)
    screen = Screen()
    assert no_external_side_effects(screen, ["sudo", "--", "approved-command"]) == 130
    assert restored == ["save", "handoff", "resume"]
    assert screen.timeouts[-1] == 250
    assert screen.clears[-1] is True


@pytest.fixture
def delete_plan(monkeypatch, panel):
    plan = SimpleNamespace(
        name=panel.manager.name,
        config_path=panel.config_path,
        workspace=panel.config.settings.workspace,
        has_service=True,
        summary=(
            f"Instance: {panel.manager.name}\n"
            "Stop, disable and remove its service.\n"
            "Delete its saved configuration.\n"
            "Workspace files will be retained.\n"
            f"Configuration: {panel.config_path}"
        ),
    )
    prepared = []

    def prepare(manager, path):
        prepared.append((manager, path))
        return plan

    monkeypatch.setattr(actions.operations, "prepare_delete_instance", prepare)
    monkeypatch.setattr(
        actions.operations,
        "delete_instance",
        lambda plan: pytest.fail("Unconfirmed deletion mutated the instance"),
    )
    return plan, prepared


@pytest.mark.parametrize("key", [10, 27, "q"])
def test_delete_confirmation_defaults_to_no_and_never_mutates(
    monkeypatch, panel, delete_plan, key
):
    _plan, prepared = delete_plan
    screen = Screen([key], height=18, width=64)
    assert actions.delete_dialog(screen, panel) is None
    assert prepared == [(panel.manager, panel.config_path)]
    assert "No, return" in screen.rendered()
    assert panel.note == "Deletion cancelled; instance unchanged."
    assert panel.manager.calls == []
    assert panel.refreshes == 0


def test_delete_uses_exact_reviewed_plan_and_never_refreshes_removed_config(
    monkeypatch, panel, delete_plan
):
    plan, prepared = delete_plan
    confirmations = []
    deleted = []

    def accept(screen, title, body, *, default):
        confirmations.append((title, body, default))
        return True

    menu_answers(monkeypatch, ["delete"])
    monkeypatch.setattr(actions, "confirm", accept)
    monkeypatch.setattr(actions.operations, "delete_instance", deleted.append)
    assert actions.actions_menu(Screen(), panel) == "instances"
    assert prepared == [(panel.manager, panel.config_path)]
    assert deleted == [plan]
    assert confirmations == [("Delete instance", plan.summary, False)]
    assert panel.note == "Instance deleted; workspace files retained."
    assert panel.refreshes == 0


def test_delete_preflight_failure_cannot_open_confirmation_or_mutate(
    monkeypatch, panel, messages
):
    menu_answers(monkeypatch, ["delete"])

    def failure(*args):
        raise ValueError("Configuration changed: " + panel.token)

    monkeypatch.setattr(actions.operations, "prepare_delete_instance", failure)
    monkeypatch.setattr(
        actions,
        "confirm",
        lambda *args, **kwargs: pytest.fail("Unsafe delete confirmed"),
    )
    assert actions.actions_menu(Screen(), panel) is None
    assert messages == [
        ("Action could not be completed", "Configuration changed: [redacted]")
    ]
    assert panel.manager.calls == []
    assert panel.refreshes == 0


def test_delete_mutation_error_keeps_panel_and_redacts_its_diagnostics(
    monkeypatch, panel, delete_plan, messages
):
    menu_answers(monkeypatch, ["delete"])
    monkeypatch.setattr(actions, "confirm", lambda *args, **kwargs: True)

    def failure(plan):
        raise ValueError("Instance changed during confirmation: " + panel.token)

    monkeypatch.setattr(actions.operations, "delete_instance", failure)
    assert actions.actions_menu(Screen(), panel) is None
    assert messages == [
        (
            "Action could not be completed",
            "Instance changed during confirmation: [redacted]",
        )
    ]
    assert "deleted" not in panel.note.lower()
    assert panel.refreshes == 0


def test_malformed_selected_entry_offers_deletion_without_lifecycle_actions(
    monkeypatch, panel, delete_plan
):
    selected = SimpleNamespace(name=panel.manager.name, config_path=panel.config_path)
    offered = []

    def select_delete(screen, title, options, **kwargs):
        offered.extend(options)
        assert kwargs["body"] == "Selected: " + selected.name
        return "delete"

    monkeypatch.setattr(actions, "choose", select_delete)
    monkeypatch.setattr(actions, "ServiceManager", lambda name: panel.manager)
    monkeypatch.setattr(actions, "confirm", lambda *args, **kwargs: True)
    deleted = []
    monkeypatch.setattr(actions.operations, "delete_instance", deleted.append)
    assert actions.actions_menu(Screen(), selected=selected) == "instances"
    assert ("delete", "Delete instance (remove configuration)") in offered
    assert not {"start", "stop", "install", "configure"} & {key for key, _ in offered}
    assert deleted == [delete_plan[0]]
    assert delete_plan[1] == [(panel.manager, panel.config_path)]


def test_system_delete_closes_only_the_authorized_panel(
    monkeypatch, panel, delete_plan
):
    monkeypatch.setattr(actions.os, "geteuid", lambda: 0)
    panel.manager.system = True
    monkeypatch.setattr(actions, "confirm", lambda *args, **kwargs: True)
    deleted = []
    monkeypatch.setattr(actions.operations, "delete_instance", deleted.append)
    assert actions.delete_dialog(Screen(), panel) == "deleted"
    assert deleted == [delete_plan[0]]
    assert panel.refreshes == 0


@pytest.mark.parametrize("deleted", [False, True])
def test_unprivileged_system_delete_uses_existing_explicit_admin_panel(
    monkeypatch, panel, delete_plan, deleted
):
    panel.manager.system = True
    panel.manager.unit_path = panel.config_path.with_suffix(".service")
    panel.config_path.write_text("test-owned configuration", encoding="utf-8")
    panel.manager.unit_path.write_text("test-owned unit", encoding="utf-8")
    opened = []

    def administrator(screen, path, name):
        opened.append((path, name))
        if deleted:
            path.unlink()
            panel.manager.unit_path.unlink()

    monkeypatch.setattr(actions, "_admin_panel", administrator)
    result = actions.delete_dialog(Screen(), panel)
    assert result == ("deleted" if deleted else None)
    assert opened == [(panel.config_path, panel.manager.name)]
    assert delete_plan[1] == []
    assert panel.config_path.exists() is not deleted
    assert panel.manager.unit_path.exists() is not deleted


def test_delete_without_selection_does_not_prepare_or_authenticate(monkeypatch):
    monkeypatch.setattr(
        actions.operations,
        "prepare_delete_instance",
        lambda *args: pytest.fail("Missing selection reached the backend"),
    )
    with pytest.raises(ValueError, match="No instance selected"):
        actions.delete_dialog(Screen())
