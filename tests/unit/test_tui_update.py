import io
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kt_mcp import tui_update as module

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux curses updater")


@pytest.fixture
def dialogs(monkeypatch):
    from kt_mcp import tui_forms

    calls = []
    choices = []
    monkeypatch.setattr(tui_forms, "choose", lambda *a, **k: choices.pop(0))
    monkeypatch.setattr(tui_forms, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(tui_forms, "message", lambda *a: calls.append(a[1:]))
    return SimpleNamespace(forms=tui_forms, choices=choices, messages=calls)


def panel(tmp_path, *, system=False, overview=False):
    return SimpleNamespace(
        manager=SimpleNamespace(name="kt-mcp-project", system=system),
        config_path=tmp_path / "private config.json",
        return_to_list=overview,
        redact=lambda text: text.replace("private-secret", "[REDACTED]"),
    )


def test_command_maps_public_options_without_shell_parsing():
    python = Path('/installation with spaces $ "quotes"/venv/bin/python')
    assert module.update_command(
        python, check=True, version="v0.4.0", name="kt-mcp-project"
    ) == [
        str(python),
        "-I",
        "-m",
        "kt_mcp",
        "update",
        "--check",
        "--version",
        "0.4.0",
        "--name",
        "kt-mcp-project",
    ]
    with pytest.raises(ValueError):
        module.update_command(python, version="0.4.0; execute")
    with pytest.raises(module.services.ManagedServiceError):
        module.update_command(python, name="other.service")


def test_output_pump_is_bounded_redacts_and_uses_isolated_subprocess(monkeypatch):
    captured = {}
    output = (
        "private-secret /mcp/url-token https://user:password@example.test\n"
        "Run kt-mcp update to install.\n"
    )

    def popen(command, **options):
        captured.update(command=command, options=options)
        return SimpleNamespace(stdout=io.StringIO(output), poll=lambda: 0)

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setenv("KT_MCP_TOKEN", "fixture-environment-token")
    monkeypatch.setenv("TUNNEL_TOKEN", "fixture-tunnel-token")
    monkeypatch.setenv("PYTHONPATH", "/not-the-installed-package")
    job = module.UpdateProcess(
        ["/venv/bin/python", "-I", "-m", "kt_mcp", "update", "--check"],
        redact=lambda text: text.replace("private-secret", "[REDACTED]"),
    )
    job.start()
    deadline = time.monotonic() + 3
    while job.poll() is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert job.poll() == 0
    shown = "\n".join(job.lines)
    assert "private-secret" not in shown and "url-token" not in shown
    assert "user:password" not in shown
    assert "Choose Install update" in shown
    assert "Run kt-mcp update" not in shown
    options = captured["options"]
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stderr"] == subprocess.STDOUT
    assert options["start_new_session"] is True
    assert "shell" not in options
    assert not {"KT_MCP_TOKEN", "TUNNEL_TOKEN", "PYTHONPATH"} & options["env"].keys()

    for index in range(1000):
        job._queue.put_nowait(str(index) + ":" + "x" * 4000)
        job.drain()
    assert job._bytes <= module.MAX_LOG_BYTES
    assert len(job.lines) <= 500
    assert job.lines[-1].startswith("999:")


def test_continuous_output_yields_to_input_before_the_producer_finishes():
    class ContinuousQueue:
        reads = 0

        def get_nowait(self):
            self.reads += 1
            assert self.reads <= 256, "A continuous child monopolized the UI loop"
            return "New output"

        def empty(self):
            return False

    job = module.UpdateProcess(["unused"])
    job._queue = ContinuousQueue()
    job.process = SimpleNamespace(poll=lambda: None)
    assert job.poll() is None
    assert 0 < job._queue.reads <= module.OUTPUT_BATCH


def test_process_completion_waits_for_remaining_retained_output():
    import queue

    job = module.UpdateProcess(["unused"])
    job.process = SimpleNamespace(poll=lambda: 0)
    job._queue = queue.Queue()
    for index in range(module.OUTPUT_BATCH + 1):
        job._queue.put_nowait(f"Output {index}")
    job._finished.set()
    assert job.poll() is None
    assert job.poll() == 0
    assert job.lines[-1] == f"Output {module.OUTPUT_BATCH}"


def test_cancellation_requests_one_graceful_interrupt_and_never_force_kills(
    monkeypatch,
):
    sent = []
    monkeypatch.setattr(
        module.os, "killpg", lambda pid, value: sent.append((pid, value))
    )
    job = module.UpdateProcess(["unused"])
    job.process = SimpleNamespace(pid=12345, poll=lambda: None)
    job.cancel()
    job.cancel()
    assert sent == [(12345, signal.SIGINT)]
    assert job.cancelled


def test_real_child_cancellation_waits_for_rollback_output():
    code = (
        "import signal,sys,time\n"
        "def restore(signum, frame):\n"
        "    print('Restoring previous environment', flush=True)\n"
        "    time.sleep(0.05)\n"
        "    print('Original service state restored', flush=True)\n"
        "    sys.exit(130)\n"
        "signal.signal(signal.SIGINT, restore)\n"
        "print('Ready for cancellation', flush=True)\n"
        "time.sleep(10)\n"
    )
    job = module.UpdateProcess([sys.executable, "-I", "-c", code])
    job.start()
    try:
        deadline = time.monotonic() + 5
        while "Ready for cancellation" not in job.lines and time.monotonic() < deadline:
            job.poll()
            time.sleep(0.01)
        assert "Ready for cancellation" in job.lines
        job.cancel()
        deadline = time.monotonic() + 5
        while job.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert job.poll() == 130
        assert job.lines[-1] == "Original service state restored"
    finally:
        job.cancel()
        job.process.wait(timeout=15)


def test_recovery_output_guides_to_native_action_without_shell_command():
    present = module._log_presenter(lambda value: value)
    assert "Recover interrupted update" in present(
        "Recovery needs attention. Saved files are retained. Run:"
    )
    assert present("/private prefix/python -I /private/worker.py --recover") == ""
    assert present("Other result") == "Other result"


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.frames = []
        self.drawn = []

    def getmaxyx(self):
        return 16, 80

    def erase(self):
        self.drawn = []

    def addnstr(self, row, column, text, limit, attribute=0):
        self.drawn.append(text[:limit])

    def refresh(self):
        self.frames.append(tuple(self.drawn))

    def getch(self):
        return next(self.keys)

    def timeout(self, value):
        pass

    def clearok(self, value):
        pass


def test_progress_stays_in_curses_until_cancellation_recovery_finishes(
    monkeypatch, dialogs
):
    from kt_mcp import tui

    events = []

    class Job:
        def __init__(self, command, **options):
            self.cancelled = False
            self.lines = ("Preparing packages",)

        def start(self):
            events.append("start")

        def poll(self):
            return 1 if self.cancelled else None

        def cancel(self):
            events.append("cancel")
            self.cancelled = True
            self.lines = (
                "Previous installation retained; original service state restored.",
            )

    monkeypatch.setattr(module, "UpdateProcess", Job)
    monkeypatch.setattr(tui, "_styles", lambda: {"title": 0, "warning": 0, "muted": 0})
    screen = Screen([ord("c"), 10])
    result = module._progress(screen, "Installing", ["fake-update"])
    assert events == ["start", "cancel"]
    assert result.returncode == 1 and result.cancelled
    assert any("restored" in line for frame in screen.frames for line in frame)


def test_check_version_selection_never_installs_or_reloads(monkeypatch, dialogs):
    dialogs.choices[:] = ["version", "check", "back"]
    monkeypatch.setattr(
        dialogs.forms,
        "run_wizard",
        lambda screen, title, callback: callback(
            ask=lambda prompt: "v0.4.0", ask_secret=None
        ),
    )
    commands = []
    monkeypatch.setattr(
        module,
        "_progress",
        lambda screen, title, command, **options: (
            commands.append(command) or module.ProcessResult(0, ("0.3.0 → 0.4.0",))
        ),
    )
    module.run_update(object())
    assert len(commands) == 1
    assert commands[0][-3:] == ["--check", "--version", "0.4.0"]
    assert dialogs.messages == []


def test_failed_install_returns_to_menu_and_does_not_reload(monkeypatch, dialogs):
    dialogs.choices[:] = ["install", "back"]
    monkeypatch.setattr(
        module,
        "_progress",
        lambda *a, **k: module.ProcessResult(1, ("Previous installation retained.",)),
    )
    module.run_update(object())
    assert dialogs.messages[-1][0] == "Operation did not complete"


@pytest.mark.parametrize(
    "system,overview,expected",
    [
        (False, False, "explicit"),
        (True, False, "system"),
        (False, True, "instance"),
    ],
)
def test_success_requests_reload_with_current_selection(
    tmp_path, monkeypatch, dialogs, system, overview, expected
):
    selected = panel(tmp_path, system=system, overview=overview)
    dialogs.choices[:] = ["install"]
    monkeypatch.setattr(
        module, "_progress", lambda *a, **k: module.ProcessResult(0, ("Updated",))
    )
    with pytest.raises(module.ReloadTUI) as reloaded:
        module.run_update(object(), selected)
    arguments = reloaded.value.argv
    if expected == "instance":
        assert arguments == ["--instance", selected.manager.name]
    else:
        assert arguments[:4] == [
            "--config",
            str(selected.config_path),
            "--name",
            selected.manager.name,
        ]
        assert ("--system" in arguments) is system
    assert not arguments or arguments[0] != "tui"


def test_service_choice_only_maps_name_and_never_stops_other_instances(
    monkeypatch, dialogs
):
    bindings = [
        SimpleNamespace(
            name="kt-mcp-one", system=False, status={"ActiveState": "active"}
        ),
        SimpleNamespace(
            name="kt-mcp-two", system=False, status={"ActiveState": "active"}
        ),
        SimpleNamespace(
            name="kt-mcp-system", system=True, status={"ActiveState": "active"}
        ),
    ]
    monkeypatch.setattr(module.services, "find_services", lambda python: bindings)
    monkeypatch.setattr(
        module.services,
        "stop_service",
        lambda *a, **k: pytest.fail("the update UI must not batch-stop instances"),
    )
    menus = []
    answers = iter(["service", "kt-mcp-two", "check", "back"])

    def choose(screen, title, options, **kwargs):
        menus.append(options)
        return next(answers)

    monkeypatch.setattr(dialogs.forms, "choose", choose)
    commands = []
    monkeypatch.setattr(
        module,
        "_progress",
        lambda screen, title, command, **options: (
            commands.append(command) or module.ProcessResult(0, ("Up to date",))
        ),
    )
    module.run_update(object())
    assert "--check" in commands[0]
    assert commands[0][-2:] == ["--name", "kt-mcp-two"]
    assert "kt-mcp-system" not in {key for key, label in menus[1]}


@pytest.mark.parametrize("cli_available", [True, False])
def test_recovery_uses_public_cli_or_validated_independent_worker(
    tmp_path, monkeypatch, cli_available
):
    installation = module.updater.Installation(
        tmp_path, tmp_path / "venv/bin/python", Path("/original python"), "0.3.0"
    )
    inspected = []
    monkeypatch.setattr(
        module.updater,
        "recoverable_installation",
        lambda prefix: inspected.append(prefix) or installation,
    )
    monkeypatch.setattr(module, "_cli_available", lambda python: cli_available)
    command = module.recovery_command(installation.python, tmp_path)
    assert inspected == [tmp_path]
    if cli_available:
        assert command == [
            str(installation.python),
            "-I",
            "-m",
            "kt_mcp",
            "update",
            "--recover",
        ]
    else:
        assert command == [
            str(installation.base_python),
            "-I",
            str(tmp_path / ".kt-mcp-update/worker.py"),
            "--recover",
        ]


def test_invalid_recovery_never_probes_or_executes_unvalidated_worker(
    tmp_path, monkeypatch
):
    def reject(prefix):
        raise module.updater.UpdateError("Recovery journal is invalid")

    monkeypatch.setattr(module.updater, "recoverable_installation", reject)
    monkeypatch.setattr(
        module,
        "_cli_available",
        lambda python: pytest.fail("validation must run first"),
    )
    with pytest.raises(module.updater.UpdateError, match="journal"):
        module.recovery_command(tmp_path / "venv/bin/python", tmp_path)


def test_recover_action_reloads_through_public_cli(monkeypatch, dialogs, tmp_path):
    monkeypatch.setattr(module.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(module.sys, "base_prefix", "/base-python")
    command = [
        str(tmp_path / "venv/bin/python"),
        "-I",
        "-m",
        "kt_mcp",
        "update",
        "--recover",
    ]
    monkeypatch.setattr(module, "recovery_command", lambda *args: command)
    observed = []
    monkeypatch.setattr(
        module,
        "_progress",
        lambda screen, title, argv, **options: (
            observed.append(argv) or module.ProcessResult(0, ("Recovery complete.",))
        ),
    )
    dialogs.choices[:] = ["recover"]
    with pytest.raises(module.ReloadTUI) as reloaded:
        module.run_update(object())
    assert reloaded.value.argv == []
    assert observed == [command]


def test_declining_update_does_not_start_a_child(monkeypatch, dialogs):
    dialogs.choices[:] = ["install", "back"]
    monkeypatch.setattr(dialogs.forms, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        module, "_progress", lambda *a, **k: pytest.fail("declined update was launched")
    )
    module.run_update(object())
