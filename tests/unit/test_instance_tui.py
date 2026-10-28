import sys
from types import SimpleNamespace

import pytest

from kt_mcp import instance_tui as module
from kt_mcp import instances, tui
from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux terminal manager"
)


class FakeManager:
    system = False

    def __init__(self, name, root):
        self.name = name
        self.unit_name = name + ".service"
        self.unit_path = root / self.unit_name
        self.calls = []
        self.log_text = ""
        self.status_data = {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": "101" if name.endswith("-a") else "202",
            "NRestarts": "0",
            "UnitFileState": "enabled",
        }

    def status(self):
        return dict(self.status_data)

    def linger_status(self):
        return "yes"

    def control(self, action):
        self.calls.append(("control", action))
        if action == "stop":
            self.status_data.update(ActiveState="inactive", MainPID="0")
        elif action in {"start", "restart"}:
            self.status_data.update(ActiveState="active")

    def install(self, path, **options):
        self.calls.append(("install", path, options))
        self.status_data["LoadState"] = "loaded"

    def logs(self, lines):
        self.calls.append(("logs", lines))
        return self.log_text


class Screen:
    def __init__(self, keys, *, height=30, width=110):
        self.keys = iter(keys)
        self.height = height
        self.width = width
        self.frames = []
        self.drawn = []

    def getmaxyx(self):
        return self.height, self.width

    def keypad(self, enabled):
        pass

    def timeout(self, value):
        pass

    def clearok(self, value):
        pass

    def erase(self):
        self.drawn = []

    def addnstr(self, row, column, text, limit, attribute=0):
        value = text[:limit]
        assert column + tui.display_width(value) < self.width
        self.drawn.append((row, value))

    def refresh(self):
        self.frames.append(list(self.drawn))

    def getch(self):
        return next(self.keys)


@pytest.fixture
def overview(tmp_path, monkeypatch):
    import curses

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    monkeypatch.setattr(tui, "ensure_available", lambda *args, **kwargs: None)
    paths = {}
    tokens = {}
    for suffix, port in (("a", 18765), ("b", 18766)):
        name = "kt-mcp-" + suffix
        workspace = tmp_path / ("workspace-" + suffix)
        workspace.mkdir()
        token = suffix * 40
        path = instances.config_path(name)
        save_config(
            DaemonConfig(
                DesktopSettings(str(workspace), mode="local", port=port), token
            ),
            path,
        )
        paths[name] = path
        tokens[name] = token
    managers = {}

    def manager(name="kt-mcp", system=False):
        assert system is False
        return managers.setdefault(name, FakeManager(name, tmp_path))

    monkeypatch.setattr(tui, "ServiceManager", manager)
    controller = module.InstanceManagerController()
    controller.refresh()
    return SimpleNamespace(
        controller=controller,
        managers=managers,
        paths=paths,
        tokens=tokens,
        tmp_path=tmp_path,
    )


def all_text(screen):
    return "\n".join(value for frame in screen.frames for _, value in frame)


def test_overview_lists_independent_metadata_and_selected_link(overview):
    screen = Screen([ord("q")], height=18, width=100)
    module.instance_screen(screen, overview.controller)
    text = all_text(screen)
    assert "Instances" in text
    assert "kt-mcp-a" in text and "kt-mcp-b" in text
    assert "18765" in text and "18766" in text
    assert "workspace-a" in text and "workspace-b" in text
    assert "http://127.0.0.1:18765/mcp/" + overview.tokens["kt-mcp-a"] in text
    assert overview.tokens["kt-mcp-b"] not in text
    assert all(not manager.calls for manager in overview.managers.values())


@pytest.mark.parametrize(
    "key,action", [("s", "start"), ("x", "stop"), ("r", "restart")]
)
def test_actions_apply_only_to_selected_instance(overview, key, action):
    controller = overview.controller
    controller.select("kt-mcp-b")
    controller.act(key)
    assert overview.managers["kt-mcp-a"].calls == []
    assert overview.managers["kt-mcp-a"].status_data["MainPID"] == "101"
    assert overview.managers["kt-mcp-b"].calls == [("control", action)]


def test_start_registers_only_selected_unregistered_instance(overview):
    controller = overview.controller
    controller.select("kt-mcp-b")
    overview.managers["kt-mcp-b"].status_data.update(
        LoadState="not-found", ActiveState="inactive", MainPID="0"
    )
    controller.act("s")
    assert overview.managers["kt-mcp-a"].calls == []
    assert overview.managers["kt-mcp-b"].calls == [
        (
            "install",
            overview.paths["kt-mcp-b"],
            {"now": True, "linger": True},
        )
    ]


def test_selection_survives_refresh_and_down_key_targets_configuration(
    overview, monkeypatch
):
    import curses

    configured = []

    def configure(screen, panel):
        configured.append(panel.manager.name)
        panel.note = "Configuration cancelled; service unchanged."

    monkeypatch.setattr(tui, "_configure", configure)
    screen = Screen([curses.KEY_DOWN, ord("o"), ord("q")])
    module.instance_screen(screen, overview.controller)
    assert configured == ["kt-mcp-b"]
    overview.controller.refresh()
    assert overview.controller.selected.name == "kt-mcp-b"
    assert all(not manager.calls for manager in overview.managers.values())


def test_detail_quit_returns_to_list_then_outer_quit_exits(overview):
    screen = Screen([10, ord("q"), ord("q")])
    module.instance_screen(screen, overview.controller)
    text = all_text(screen)
    assert "[q] Instances" in text
    assert (
        sum(any("Instances" in line for _, line in frame) for frame in screen.frames)
        >= 2
    )
    assert all(not manager.calls for manager in overview.managers.values())


def test_malformed_entry_remains_visible_and_other_instances_work(overview):
    path = instances.config_path("broken")
    path.write_text("{invalid-json", encoding="utf-8")
    path.chmod(0o600)
    controller = overview.controller
    controller.refresh()
    broken = next(item for item in controller.items if item.name == "kt-mcp-broken")
    assert broken.error
    assert controller.state(broken) == "invalid"
    controller.select(broken.name)
    with pytest.raises(ValueError, match="configuration is unavailable"):
        controller.act("s")
    controller.select("kt-mcp-b")
    controller.act("x")
    assert overview.managers["kt-mcp-a"].calls == []
    assert overview.managers["kt-mcp-b"].calls == [("control", "stop")]


def test_one_status_error_does_not_hide_others_or_expose_credentials(
    overview, monkeypatch
):
    token = overview.tokens["kt-mcp-b"]

    def unavailable():
        raise tui.ServiceError("fixture status error " + token)

    monkeypatch.setattr(overview.managers["kt-mcp-b"], "status", unavailable)
    overview.controller.refresh()
    states = {
        item.name: overview.controller.state(item) for item in overview.controller.items
    }
    assert states == {"kt-mcp-a": "active", "kt-mcp-b": "unavailable"}
    assert token not in overview.controller.errors["kt-mcp-b"]


def test_empty_overview_does_not_open_a_setup_wizard(overview, monkeypatch):
    monkeypatch.setattr(instances, "list_instances", list)
    monkeypatch.setattr(
        instances,
        "setup_instance",
        lambda **kwargs: pytest.fail("opening an empty list must not create anything"),
    )
    screen = Screen([ord("q")], height=18, width=64)
    module.instance_screen(screen, module.InstanceManagerController())
    assert "No instances yet. Press n" in all_text(screen)


def test_small_terminal_ignores_start_configure_and_create_keys(overview, monkeypatch):
    monkeypatch.setattr(
        instances,
        "setup_instance",
        lambda **kwargs: pytest.fail("hidden create action"),
    )
    screen = Screen([ord("s"), ord("o"), ord("n"), ord("q")], height=10, width=40)
    module.instance_screen(screen, overview.controller)
    assert all(not manager.calls for manager in overview.managers.values())


def test_new_instance_selection_does_not_start_any_service(overview, monkeypatch):
    created = instances.get_instance("b")
    calls = []

    def setup(**options):
        calls.append(options)
        return created

    monkeypatch.setattr(instances, "setup_instance", setup)
    assert overview.controller.create() == created
    assert overview.controller.selected.name == created.name
    assert calls == [{"ask": None, "ask_secret": None}]
    assert all(not manager.calls for manager in overview.managers.values())


def test_configuration_cancel_pauses_only_selected_instance(
    overview, monkeypatch, capsys
):
    controller = overview.controller
    controller.select("kt-mcp-b")
    panel = controller.selected_controller()
    before = {name: path.read_bytes() for name, path in overview.paths.items()}
    binding = SimpleNamespace(name="kt-mcp-b")
    stopped = []

    def stop(current):
        stopped.append(current.name)
        overview.managers[current.name].status_data.update(
            ActiveState="inactive", MainPID="0"
        )

    monkeypatch.setattr(panel, "_edit_binding", lambda: binding)
    monkeypatch.setattr(tui, "assert_external_process", lambda current: None)
    monkeypatch.setattr(tui, "stop_service", stop)
    responses = iter(["y", ":q"])
    panel.configure(ask=lambda prompt: next(responses))
    assert stopped == ["kt-mcp-b"]
    assert overview.managers["kt-mcp-a"].status_data["MainPID"] == "101"
    assert overview.managers["kt-mcp-b"].status_data["MainPID"] == "0"
    assert all(
        path.read_bytes() == before[name] for name, path in overview.paths.items()
    )
    assert "Only instance kt-mcp-b will be paused." in capsys.readouterr().out
    assert "[s] Start" in panel.note


def test_conflicting_configuration_is_not_saved_or_applied_to_another_instance(
    overview, monkeypatch
):
    controller = overview.controller
    controller.select("kt-mcp-b")
    panel = controller.selected_controller()
    before = {name: path.read_bytes() for name, path in overview.paths.items()}
    binding = SimpleNamespace(name="kt-mcp-b")

    def stop(current):
        overview.managers[current.name].status_data.update(
            ActiveState="inactive", MainPID="0"
        )

    monkeypatch.setattr(panel, "_edit_binding", lambda: binding)
    monkeypatch.setattr(tui, "assert_external_process", lambda current: None)
    monkeypatch.setattr(tui, "stop_service", stop)
    monkeypatch.setattr(tui, "ensure_available", instances.ensure_available)
    responses = iter(["y", "", "18765", "", ""])
    with pytest.raises(instances.InstanceError, match="reserved by kt-mcp-a"):
        panel.configure(ask=lambda prompt: next(responses))
    assert all(
        path.read_bytes() == before[name] for name, path in overview.paths.items()
    )
    assert overview.managers["kt-mcp-a"].status_data["MainPID"] == "101"
    assert overview.managers["kt-mcp-b"].status_data["MainPID"] == "0"
    # The failure must release publication ownership for a later valid edit.
    with instances.catalog_lock():
        pass


@pytest.mark.parametrize("key", ["i", "s", "r"])
@pytest.mark.parametrize("active,check_socket", [("active", False), ("inactive", True)])
def test_start_guards_check_only_this_config_and_probe_only_stopped_ports(
    overview, monkeypatch, key, active, check_socket
):
    panel = overview.controller.selected_controller()
    manager = overview.managers["kt-mcp-a"]
    manager.status_data["ActiveState"] = active
    checked = []
    monkeypatch.setattr(
        tui,
        "ensure_available",
        lambda config, **options: checked.append((config.settings.port, options)),
    )
    panel.act(key)
    assert checked == [
        (18765, {"exclude": overview.paths["kt-mcp-a"], "check_socket": check_socket})
    ]


def test_conflict_blocks_post_configuration_start_before_service_mutation(
    overview, monkeypatch
):
    panel = overview.controller.selected_controller()
    panel._configuration_paused = True
    manager = overview.managers["kt-mcp-a"]
    manager.status_data["ActiveState"] = "inactive"

    def conflict(*args, **kwargs):
        raise instances.InstanceError("fixture reserved port")

    monkeypatch.setattr(tui, "ensure_available", conflict)
    monkeypatch.setattr(
        tui,
        "start_service",
        lambda binding: pytest.fail("conflicting instance started"),
    )
    with pytest.raises(instances.InstanceError, match="reserved port"):
        panel.act("s")
    assert manager.calls == []
    assert panel._configuration_paused is True


def test_saved_credentials_win_over_shell_tokens_in_selected_link(
    overview, monkeypatch
):
    monkeypatch.setenv("KT_MCP_TOKEN", "different-environment-token-" + "z" * 32)
    overview.controller.refresh()
    panel = overview.controller.selected_controller()
    assert panel.connection_url.endswith("/" + overview.tokens["kt-mcp-a"])


def terminal_route(monkeypatch):
    import curses

    captured = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(curses, "wrapper", lambda *args: captured.append(args))
    return captured


def test_plain_tui_routes_to_unified_list_without_setup(overview, monkeypatch):
    captured = terminal_route(monkeypatch)
    monkeypatch.setattr(
        instances,
        "setup_instance",
        lambda **kwargs: pytest.fail("plain tui must not run setup"),
    )
    assert tui.main([]) == 0
    assert captured[0][0] is module.instance_screen
    assert isinstance(captured[0][1], module.InstanceManagerController)
    assert captured[0][2] is False


def test_workspace_route_selects_existing_instance_without_creation(
    overview, monkeypatch
):
    captured = terminal_route(monkeypatch)
    item = instances.get_instance("b")
    monkeypatch.setattr(
        instances,
        "setup_instance",
        lambda **kwargs: pytest.fail("existing workspace duplicated"),
    )
    assert tui.main(["--workspace", item.workspace]) == 0
    assert captured[0][1]._requested_name == item.name
    assert captured[0][2] is True


def test_workspace_route_creates_missing_instance_then_opens_selected_detail(
    overview, monkeypatch
):
    captured = terminal_route(monkeypatch)
    target = overview.tmp_path / "new-workspace"
    target.mkdir()
    created = instances.get_instance("a")
    calls = []

    def setup(**options):
        calls.append(options)
        return created

    monkeypatch.setattr(instances, "setup_instance", setup)
    assert tui.main(["--workspace", str(target)]) == 0
    assert calls == [{"workspace": target}]
    assert captured[0][1]._requested_name == created.name
    assert captured[0][2] is True


def test_instance_route_selects_requested_name(overview, monkeypatch):
    captured = terminal_route(monkeypatch)
    assert tui.main(["--instance", "b"]) == 0
    assert captured[0][1]._requested_name == "kt-mcp-b"
    assert captured[0][2] is True


def test_explicit_config_keeps_the_single_panel_route(overview, monkeypatch):
    captured = terminal_route(monkeypatch)
    path = overview.paths["kt-mcp-b"]
    assert tui.main(["--config", str(path), "--name", "kt-mcp-b"]) == 0
    assert captured[0][0] is tui._screen
    panel = captured[0][1]
    assert panel.config_path == path
    assert panel.manager.name == "kt-mcp-b"
    assert panel.return_to_list is False


def test_incompatible_instance_and_explicit_config_fail_before_any_panel(
    overview, monkeypatch, capsys
):
    captured = terminal_route(monkeypatch)
    with pytest.raises(SystemExit) as error:
        tui.main(["--instance", "b", "--config", str(overview.paths["kt-mcp-b"])])
    assert error.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err
    assert captured == []
