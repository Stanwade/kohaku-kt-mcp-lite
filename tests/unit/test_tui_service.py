import sys
import threading
import time
from types import SimpleNamespace

import pytest

from kt_mcp import tui, tui_service
from kt_mcp.managed_service import StartupWaitCancelled

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux terminal UI")


class Screen:
    def __init__(self, read):
        self.read = read
        self.owner = threading.get_ident()
        self.height, self.width = 18, 64
        self.frames = []
        self.lines = []
        self.timeouts = []

    def owned(self):
        assert threading.get_ident() == self.owner, "worker touched curses"

    def getmaxyx(self):
        self.owned()
        return self.height, self.width

    def erase(self):
        self.owned()
        self.lines = []

    def addnstr(self, row, column, text, limit, attribute=0):
        self.owned()
        assert 0 <= row < self.height
        assert column + tui.display_width(text[:limit]) < self.width
        self.lines.append(text[:limit])

    def refresh(self):
        self.owned()
        self.frames.append("\n".join(self.lines))

    def getch(self):
        self.owned()
        value = self.read(self)
        if isinstance(value, BaseException):
            raise value
        return ord(value) if isinstance(value, str) else value

    def timeout(self, value):
        self.owned()
        self.timeouts.append(value)

    def keypad(self, value):
        self.owned()

    def clearok(self, value):
        self.owned()


@pytest.fixture(autouse=True)
def terminal(monkeypatch):
    import curses

    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)


@pytest.fixture
def panel(tmp_path):
    return SimpleNamespace(
        manager=SimpleNamespace(name="kt-mcp-selected"),
        config_path=tmp_path / "selected.json",
        status={"ActiveState": "inactive"},
        note="",
        _configuration_paused=True,
        redact=lambda text: str(text).replace("private-test-token", "[redacted]"),
    )


def test_slow_start_draws_before_dispatch_and_keeps_resizing(monkeypatch, panel):
    entered = threading.Event()
    release = threading.Event()
    workers = []
    reads = []

    def control(manager, path, action, *, on_progress, cancel):
        assert "Starting instance" in screen.frames[0]
        assert (manager, path, action) == (panel.manager, panel.config_path, "start")
        workers.append(threading.current_thread())
        on_progress("Waiting for MCP startup...")
        entered.set()
        assert release.wait(2)
        return {"ActiveState": "active", "MainPID": "123"}

    def read(screen):
        assert entered.wait(1)
        reads.append(True)
        if len(reads) == 1:
            screen.height, screen.width = 10, 40
        else:
            release.set()
        return -1

    screen = Screen(read)
    monkeypatch.setattr(tui_service.operations, "control_service", control)
    tui_service.run_action(screen, panel, "start")
    assert len(reads) >= 2
    assert any("Waiting for MCP startup" in frame for frame in screen.frames)
    assert all("Elapsed:" in frame for frame in screen.frames)
    assert panel.status == {"ActiveState": "active", "MainPID": "123"}
    assert panel.note == "MCP started; local connection verified."
    assert panel._configuration_paused is False
    assert all(not worker.is_alive() for worker in workers)
    assert screen.timeouts[-1] == 250


@pytest.mark.parametrize("key", ["q", "\x1b", "\x03", KeyboardInterrupt()])
def test_return_cancels_only_readiness_and_joins_worker(monkeypatch, panel, key):
    started = threading.Event()
    finished = threading.Event()
    service = {"active": False}
    workers = []

    def control(manager, path, action, *, on_progress, cancel):
        assert action == "start"
        workers.append(threading.current_thread())
        service["active"] = True
        started.set()
        deadline = time.monotonic() + 2
        while not cancel():
            assert time.monotonic() < deadline, "UI did not request return"
            time.sleep(0.005)
        finished.set()
        raise StartupWaitCancelled("Stopped waiting for MCP startup")

    keys = iter([key])

    def read(screen):
        assert started.wait(1)
        return next(keys, -1)

    screen = Screen(read)
    monkeypatch.setattr(tui_service.operations, "control_service", control)
    assert tui_service.run_action(screen, panel, "start") is None
    assert service["active"] is True
    assert finished.is_set()
    assert panel.note == "Startup wait ended; check the instance status or logs."
    assert panel._configuration_paused is False
    assert all(not worker.is_alive() for worker in workers)
    assert screen.timeouts[-1] == 250


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_backend_failure_restores_terminal_and_redacts_note(monkeypatch, panel, action):
    workers = []

    def control(*args, **kwargs):
        workers.append(threading.current_thread())
        raise ValueError("failed at private-test-token")

    screen = Screen(lambda screen: -1)
    monkeypatch.setattr(tui_service.operations, "control_service", control)
    with pytest.raises(ValueError):
        tui_service.run_action(screen, panel, action)
    assert "private-test-token" not in panel.note
    assert "[redacted]" in panel.note
    assert all(not worker.is_alive() for worker in workers)
    assert screen.timeouts[-1] == 250


def test_stop_remains_responsive_until_its_command_finishes(monkeypatch, panel):
    entered = threading.Event()
    release = threading.Event()
    reads = []

    def control(manager, path, action, *, on_progress, cancel):
        assert action == "stop"
        assert cancel is None
        on_progress("Stopping the instance...")
        entered.set()
        assert release.wait(2)
        return {"ActiveState": "inactive", "MainPID": "0"}

    def read(screen):
        assert entered.wait(1)
        reads.append(True)
        if len(reads) == 1:
            return "q"
        release.set()
        return -1

    screen = Screen(read)
    monkeypatch.setattr(tui_service.operations, "control_service", control)
    tui_service.run_action(screen, panel, "stop")
    assert len(reads) >= 2
    assert panel.note == "MCP stopped."
    assert panel.status["ActiveState"] == "inactive"
    assert any("Elapsed:" in frame for frame in screen.frames)
    assert all("service keeps starting" not in frame for frame in screen.frames)


def test_ctrl_c_during_completion_poll_returns_without_exiting_tui(monkeypatch, panel):
    real_wait = threading.Event.wait
    interrupted = []
    service = {"active": False}
    finished = threading.Event()
    ui_thread = threading.get_ident()

    def control(manager, path, action, *, on_progress, cancel):
        service["active"] = True
        deadline = time.monotonic() + 2
        while not cancel():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        finished.set()
        raise StartupWaitCancelled("Stopped waiting")

    def wait(event, timeout=None):
        if threading.get_ident() == ui_thread and timeout == 0.025 and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return real_wait(event, timeout)

    monkeypatch.setattr(tui_service.operations, "control_service", control)
    monkeypatch.setattr(threading.Event, "wait", wait)
    screen = Screen(lambda screen: -1)
    assert tui_service.run_action(screen, panel, "start") is None
    assert interrupted == [True]
    assert finished.is_set()
    assert service["active"] is True
    assert panel.note.startswith("Startup wait ended")


def test_unexpected_ui_exit_waits_for_backend_completion_event(monkeypatch, panel):
    finished = threading.Event()

    def control(manager, path, action, *, on_progress, cancel):
        deadline = time.monotonic() + 2
        while not cancel():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        time.sleep(0.05)
        finished.set()
        raise StartupWaitCancelled("Stopped waiting")

    def read(screen):
        return RuntimeError("terminal closed")

    monkeypatch.setattr(tui_service.operations, "control_service", control)
    monkeypatch.setattr(threading.Thread, "is_alive", lambda thread: False)
    with pytest.raises(RuntimeError, match="terminal closed"):
        tui_service.run_action(Screen(read), panel, "start")
    assert finished.is_set()
