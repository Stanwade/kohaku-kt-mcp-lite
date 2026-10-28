import builtins
import sys
from types import SimpleNamespace

import pytest

from kt_mcp import cli, onboarding, tui
from kt_mcp.daemon import DaemonConfig, load_config
from kt_mcp.desktop import DesktopSettings

TOKEN = "interactive-cli-test-token-" + "a" * 32


def no_input(*args, **kwargs):
    pytest.fail("this CLI path must not read interactive input")


def terminal(monkeypatch, stdin=True, stdout=True):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)
    monkeypatch.setattr(builtins, "input", no_input)


@pytest.fixture
def entrypoints(monkeypatch):
    from kt_mcp import runtime as runtime_module
    from kt_mcp import server as server_module

    calls = SimpleNamespace(tui=[], runtimes=[], stdio=[])

    def open_tui(arguments):
        calls.tui.append(arguments)
        return 17

    def make_runtime(config):
        runtime = object()
        calls.runtimes.append((config, runtime))
        return runtime

    async def serve_protocol(runtime):
        calls.stdio.append(runtime)

    monkeypatch.setattr(tui, "main", open_tui)
    # The CLI imports protocol dependencies lazily inside main().
    monkeypatch.setattr(runtime_module, "Runtime", make_runtime)
    monkeypatch.setattr(server_module, "serve_stdio", serve_protocol)
    return calls


@pytest.mark.parametrize("use_process_argv", [False, True])
def test_linux_bare_command_in_a_terminal_opens_tui(
    monkeypatch, entrypoints, use_process_argv
):
    monkeypatch.setattr(sys, "platform", "linux")
    terminal(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["kt-mcp"])

    result = cli.main() if use_process_argv else cli.main([])

    assert result == 17
    assert entrypoints.tui == [[]]
    assert entrypoints.runtimes == []
    assert entrypoints.stdio == []


@pytest.mark.parametrize(
    "platform,stdin_tty,stdout_tty",
    [
        ("linux", False, False),
        ("linux", True, False),
        ("linux", False, True),
        ("win32", True, True),
        ("darwin", True, True),
    ],
    ids=["pipe", "stdin-only", "stdout-only", "windows", "macos"],
)
def test_bare_protocol_launch_is_preserved_outside_dual_tty_linux(
    monkeypatch, capsys, entrypoints, platform, stdin_tty, stdout_tty
):
    monkeypatch.setattr(sys, "platform", platform)
    terminal(monkeypatch, stdin_tty, stdout_tty)

    assert cli.main([]) == 0

    assert entrypoints.tui == []
    assert len(entrypoints.runtimes) == 1
    assert entrypoints.stdio == [entrypoints.runtimes[0][1]]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "arguments",
    [
        ["--workspace", "."],
        ["--transport", "stdio"],
        ["--workspace", ".", "--transport", "stdio"],
        ["--port", "9123"],
    ],
    ids=["workspace", "explicit-stdio", "workspace-and-stdio", "other-explicit-option"],
)
def test_explicit_server_arguments_never_trigger_automatic_tui(
    monkeypatch, capsys, entrypoints, arguments
):
    monkeypatch.setattr(sys, "platform", "linux")
    terminal(monkeypatch)

    assert cli.main(arguments) == 0

    assert entrypoints.tui == []
    assert len(entrypoints.runtimes) == 1
    assert entrypoints.stdio == [entrypoints.runtimes[0][1]]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "option,expected_text", [("--help", "usage:"), ("--version", "Kohaku KT MCP")]
)
def test_explicit_help_and_version_still_exit_without_starting_a_service(
    monkeypatch, capsys, entrypoints, option, expected_text
):
    monkeypatch.setattr(sys, "platform", "linux")
    terminal(monkeypatch)

    with pytest.raises(SystemExit) as result:
        cli.main([option])

    assert result.value.code == 0
    assert expected_text in capsys.readouterr().out
    assert entrypoints.tui == []
    assert entrypoints.runtimes == []
    assert entrypoints.stdio == []


@pytest.mark.parametrize("tty", [True, False], ids=["terminal", "pipe"])
def test_init_with_explicit_workspace_stays_noninteractive(
    tmp_path, monkeypatch, entrypoints, tty
):
    monkeypatch.setattr(sys, "platform", "linux")
    terminal(monkeypatch, tty, tty)
    monkeypatch.setattr(onboarding, "setup_config", no_input)
    path = tmp_path / "private/config.json"

    assert (
        cli.main(
            [
                "init",
                "--workspace",
                ".",
                "--config",
                str(path),
                "--port",
                "9123",
                "--mode",
                "quick",
                "--cloudflared",
                "/custom/bin/cloudflared",
            ]
        )
        == 0
    )

    loaded = load_config(path)
    assert loaded.settings == DesktopSettings(
        str(tmp_path.resolve()), 9123, "quick", cloudflared="/custom/bin/cloudflared"
    )
    assert entrypoints.tui == []
    assert entrypoints.runtimes == []
    assert entrypoints.stdio == []


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ([], DesktopSettings(mode="local")),
        (
            [
                "--port",
                "9123",
                "--mode",
                "named",
                "--public-origin",
                "https://mcp.example.com",
                "--cloudflared",
                "/custom/bin/cloudflared",
            ],
            DesktopSettings(
                "",
                9123,
                "named",
                "https://mcp.example.com",
                "/custom/bin/cloudflared",
            ),
        ),
    ],
    ids=["defaults", "explicit-options"],
)
def test_init_without_workspace_uses_wizard_with_all_selected_defaults(
    tmp_path, monkeypatch, entrypoints, arguments, expected
):
    monkeypatch.setattr(sys, "platform", "linux")
    terminal(monkeypatch)
    path = tmp_path / "private/config.json"
    calls = []

    def setup(config_path, *, defaults):
        calls.append((config_path, defaults))
        return DaemonConfig(DesktopSettings(str(tmp_path), mode="local"), TOKEN)

    monkeypatch.setattr(onboarding, "setup_config", setup)

    assert cli.main(["init", "--config", str(path), *arguments]) == 0

    assert calls == [(path, expected)]
    assert not path.exists(), "the CLI must leave configuration writes to the wizard"
    assert entrypoints.tui == []
    assert entrypoints.runtimes == []
    assert entrypoints.stdio == []


@pytest.mark.parametrize(
    "stdin_tty,stdout_tty",
    [(False, False), (True, False), (False, True)],
    ids=["pipe", "stdin-only", "stdout-only"],
)
def test_init_without_workspace_rejects_noninteractive_input_immediately(
    tmp_path, monkeypatch, capsys, entrypoints, stdin_tty, stdout_tty
):
    monkeypatch.setattr(sys, "platform", "linux")
    terminal(monkeypatch, stdin_tty, stdout_tty)
    monkeypatch.setattr(sys.stdin, "read", no_input)
    monkeypatch.setattr(sys.stdin, "readline", no_input)
    monkeypatch.setattr(onboarding, "setup_config", no_input)
    path = tmp_path / "private/config.json"

    with pytest.raises(SystemExit) as result:
        cli.main(["init", "--config", str(path), "--port", "9123"])

    assert result.value.code == 2
    output = capsys.readouterr()
    assert "--workspace ." in output.err
    assert output.out == ""
    assert not path.parent.exists()
    assert entrypoints.tui == []
    assert entrypoints.runtimes == []


def test_init_wizard_cancellation_is_a_clean_exit_with_an_explanation(
    tmp_path, monkeypatch, capsys, entrypoints
):
    monkeypatch.setattr(sys, "platform", "linux")
    terminal(monkeypatch)
    path = tmp_path / "private/config.json"
    message = "Setup cancelled; no configuration was saved."

    def cancel(*args, **kwargs):
        raise onboarding.SetupCancelled(message)

    monkeypatch.setattr(onboarding, "setup_config", cancel)

    assert cli.main(["init", "--config", str(path)]) == 0

    output = capsys.readouterr()
    assert message in output.out
    assert output.err == ""
    assert not path.parent.exists()
    assert entrypoints.tui == []
    assert entrypoints.runtimes == []


@pytest.mark.parametrize("with_workspace", [False, True])
def test_init_is_still_linux_only_before_any_prompt_or_write(
    tmp_path, monkeypatch, capsys, entrypoints, with_workspace
):
    monkeypatch.setattr(sys, "platform", "win32")
    terminal(monkeypatch)
    monkeypatch.setattr(onboarding, "setup_config", no_input)
    path = tmp_path / "private/config.json"
    arguments = ["init", "--config", str(path)]
    if with_workspace:
        arguments.extend(["--workspace", "."])

    with pytest.raises(SystemExit) as result:
        cli.main(arguments)

    assert result.value.code == 2
    assert "Linux" in capsys.readouterr().err
    assert not path.parent.exists()
    assert entrypoints.tui == []
    assert entrypoints.runtimes == []
