"""Regression coverage for commands launched from a minimal service environment."""

import asyncio
import json
import os
import shlex
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kt_mcp.config import Config
from kt_mcp.processes import execute, find_bash

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux user Bash startup semantics"
)


@pytest.fixture
def shell_home(tmp_path, monkeypatch):
    try:
        find_bash()
    except ValueError:
        pytest.skip("Bash is not installed")
    home = tmp_path / "user home"
    workspace = tmp_path / "workspace with spaces"
    binary = home / "user-bin"
    binary.mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TERM", "dumb")
    for key in tuple(os.environ):
        if key.startswith(("KT_TEST_", "BASH_FUNC_")) or key in {
            "BASH_ENV",
            "ENV",
            "BASHOPTS",
            "SHELLOPTS",
            "CDPATH",
            "PROMPT_COMMAND",
            "PS1",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
        }:
            monkeypatch.delenv(key, raising=False)
    rc = home / ".bashrc"
    rc.write_text(
        """case $- in *i*) ;; *) return ;; esac
export KT_TEST_RC_COUNT=$(( ${KT_TEST_RC_COUNT:-0} + 1 ))
export KT_TEST_PROFILE_VALUE=first
export PATH="$HOME/user-bin:$PATH"
proxy_on() {
    export HTTPS_PROXY=http://127.0.0.1:18443
    export https_proxy="$HTTPS_PROXY"
}
alias pon=proxy_on
pon
kt_fixture_function() { printf function-ok; }
alias kt_fixture_alias='printf alias-ok'
printf 'startup banner\\n'
cd -- "$HOME"
""",
        encoding="utf-8",
    )
    for name, value in (
        (".bash_profile", "bash_profile"),
        (".bash_login", "bash_login"),
        (".profile", "profile"),
    ):
        (home / name).write_text(
            f"export KT_TEST_LOGIN_FILE={value}\n"
            '[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"\n',
            encoding="utf-8",
        )
    codex = binary / "codex"
    codex.write_text("#!/bin/sh\nprintf 'fixture-codex\\n'\n", encoding="utf-8")
    codex.chmod(0o700)
    # A profile-selected Python must not replace the dedicated MCP interpreter.
    fake_python = binary / "python"
    fake_python.write_text(
        "#!/bin/sh\nprintf 'wrong-interpreter\\n'\nexit 93\n", encoding="utf-8"
    )
    fake_python.chmod(0o700)
    return SimpleNamespace(
        home=home,
        workspace=workspace,
        binary=binary,
        rc=rc,
        profile=home / ".bash_profile",
        config=Config(workspace),
    )


async def test_bash_loads_user_commands_functions_aliases_and_proxy(shell_home):
    result = await execute(
        shell_home.config,
        "bash",
        """printf '%s\\n' "$KT_TEST_LOGIN_FILE" "$KT_TEST_RC_COUNT" "$HTTPS_PROXY"
kt_fixture_function
printf '\\n'
kt_fixture_alias
printf '\\n'
codex --version
pwd -P
""",
    )
    assert result["exit_code"] == 0, result
    assert result["output"].splitlines() == [
        "bash_profile",
        "1",
        "http://127.0.0.1:18443",
        "function-ok",
        "alias-ok",
        "fixture-codex",
        str(shell_home.workspace.resolve()),
    ]
    assert "startup banner\n" in result.get("startup_output", "")


async def test_bash_preserves_exit_status_without_logout_output_or_history(shell_home):
    history = shell_home.home / "command-history"
    shell_home.profile.write_text(
        shell_home.profile.read_text(encoding="utf-8")
        + 'HISTFILE="$HOME/command-history"\n'
        + "HISTSIZE=100\nHISTFILESIZE=100\nset -o history\n"
        + "history -s startup-history-record\n",
        encoding="utf-8",
    )
    (shell_home.home / ".bash_logout").write_text(
        "printf 'logout banner\\n'\n", encoding="utf-8"
    )
    result = await execute(shell_home.config, "bash", "printf command-finished; exit 7")
    assert result["exit_code"] == 7, result
    assert result["output"] == "command-finished"
    assert "error" not in result, result
    assert not history.exists()


async def test_python_keeps_its_interpreter_and_receives_initialized_environment(
    shell_home,
):
    result = await execute(
        shell_home.config,
        "python",
        """import json, os, shutil, sys
print(json.dumps({
    "executable": sys.executable,
    "prefix": sys.prefix,
    "cwd": os.getcwd(),
    "proxy": os.environ.get("HTTPS_PROXY"),
    "lower_proxy": os.environ.get("https_proxy"),
    "startup_count": os.environ.get("KT_TEST_RC_COUNT"),
    "codex": shutil.which("codex"),
    "path_python": shutil.which("python"),
}))
""",
    )
    assert result["exit_code"] == 0, result
    # Startup output must not corrupt normal machine-readable command output.
    data = json.loads(result["output"])
    assert Path(data["executable"]).absolute() == Path(sys.executable).absolute()
    assert Path(data["prefix"]).absolute() == Path(sys.prefix).absolute()
    assert Path(data["cwd"]) == shell_home.workspace.resolve()
    assert data["proxy"] == data["lower_proxy"] == "http://127.0.0.1:18443"
    assert data["startup_count"] == "1"
    assert Path(data["codex"]) == shell_home.binary / "codex"
    assert Path(data["path_python"]) == shell_home.binary / "python"


@pytest.mark.parametrize(
    ("remove", "selected"),
    [
        ((), "bash_profile"),
        ((".bash_profile",), "bash_login"),
        ((".bash_profile", ".bash_login"), "profile"),
    ],
)
async def test_first_available_login_profile_is_used(shell_home, remove, selected):
    for name in remove:
        (shell_home.home / name).unlink()
    result = await execute(
        shell_home.config,
        "bash",
        'printf "%s:%s" "$KT_TEST_LOGIN_FILE" "$KT_TEST_RC_COUNT"',
    )
    assert result["exit_code"] == 0, result
    assert result["output"] == selected + ":1"


async def test_login_profile_controls_whether_bashrc_is_sourced(shell_home):
    shell_home.profile.write_text(
        "export KT_TEST_LOGIN_FILE=chosen\n", encoding="utf-8"
    )
    result = await execute(
        shell_home.config,
        "bash",
        'printf "%s:%s" "$KT_TEST_LOGIN_FILE" "${KT_TEST_RC_COUNT-unset}"',
    )
    assert result["exit_code"] == 0, result
    assert result["output"] == "chosen:unset"


async def test_each_call_gets_current_profiles_and_independent_shell_state(shell_home):
    first = await execute(
        shell_home.config,
        "bash",
        """export KT_TEST_EPHEMERAL=previous-call
kt_temporary_function() { :; }
cd -- "$HOME"
printf changed
""",
    )
    assert first["exit_code"] == 0, first
    assert first["output"] == "changed"
    shell_home.rc.write_text(
        shell_home.rc.read_text(encoding="utf-8").replace(
            "KT_TEST_PROFILE_VALUE=first", "KT_TEST_PROFILE_VALUE=second"
        ),
        encoding="utf-8",
    )
    second = await execute(
        shell_home.config,
        "bash",
        """printf '%s\\n' "$KT_TEST_PROFILE_VALUE" "${KT_TEST_EPHEMERAL-unset}" "$KT_TEST_RC_COUNT"
if type kt_temporary_function >/dev/null 2>&1; then
    printf 'leaked-function\\n'
else
    printf 'fresh-functions\\n'
fi
pwd -P
""",
    )
    assert second["exit_code"] == 0, second
    assert second["output"].splitlines() == [
        "second",
        "unset",
        "1",
        "fresh-functions",
        str(shell_home.workspace.resolve()),
    ]


@pytest.mark.parametrize("kind", ["bash", "python"])
async def test_startup_output_has_a_separate_bounded_budget(shell_home, kind):
    shell_home.profile.write_text(
        "printf '%s' '" + "x" * 4096 + "'\n", encoding="utf-8"
    )
    result = await execute(
        Config(shell_home.workspace, max_output_bytes=32),
        kind,
        "printf command-ok" if kind == "bash" else "print('command-ok', end='')",
    )
    assert result["exit_code"] == 0, result
    assert result["output"] == "command-ok"
    assert not result["truncated"]
    assert result["startup_output"]
    assert len(result["startup_output"].encode("utf-8")) <= 32
    assert result["startup_truncated"]


@pytest.mark.parametrize("kind", ["bash", "python"])
@pytest.mark.parametrize("profile_exit", [0, 23])
async def test_profile_exit_prevents_the_command_from_running(
    shell_home, kind, profile_exit
):
    shell_home.profile.write_text(
        f"printf 'startup ended early\\n' >&2\nexit {profile_exit}\n", encoding="utf-8"
    )
    marker = shell_home.workspace / "should-not-run"
    source = (
        "printf executed > should-not-run"
        if kind == "bash"
        else "from pathlib import Path; Path('should-not-run').write_text('executed')"
    )
    result = await execute(shell_home.config, kind, source, timeout=5)
    assert "shell initialization" in result.get("error", "").lower(), result
    assert "startup ended early" in result.get("startup_output", "")
    assert not marker.exists()


def owned_child(shell_home):
    ready = shell_home.home / "child-ready.json"
    script = shell_home.home / "owned-child.py"
    script.write_text(
        "import json, os, time\n"
        "from pathlib import Path\n"
        f"Path({str(ready)!r}).write_text(json.dumps({{'pid': os.getpid()}}))\n"
        "while True:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    return script, ready


async def wait_for_child(ready, task):
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if ready.exists():
            try:
                return json.loads(ready.read_text())["pid"]
            except (ValueError, KeyError):
                pass
        if task.done():
            pytest.fail("Command finished before its startup child became ready")
        await asyncio.sleep(0.02)
    pytest.fail("Owned child did not start")


def process_running(pid):
    try:
        status = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    # A killed descendant may briefly wait for the host init process to reap it.
    return status.rsplit(")", 1)[1].split()[0] != "Z"


async def assert_child_stopped(pid):
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        if not process_running(pid):
            return
        await asyncio.sleep(0.02)
    assert not process_running(pid), "Owned command descendant survived cleanup"


def cleanup_owned_child(ready, script):
    """Clean up only the unique child created by a failing regression test."""
    if not ready.exists():
        return
    try:
        pid = json.loads(ready.read_text())["pid"]
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if os.fsencode(script) in arguments:
            os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError, KeyError):
        pass


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descendant ownership checks")
async def test_timeout_covers_startup_and_cleans_its_child(shell_home):
    script, ready = owned_child(shell_home)
    shell_home.profile.write_text(
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} &\nwait\n",
        encoding="utf-8",
    )
    try:
        result = await execute(
            shell_home.config,
            "python",
            "from pathlib import Path; Path('command-ran').touch()",
            timeout=2,
        )
        assert "timed out" in result.get("error", "").lower(), result
        assert ready.exists(), "The timeout should exercise a running startup child"
        assert not (shell_home.workspace / "command-ran").exists()
        await assert_child_stopped(json.loads(ready.read_text())["pid"])
    finally:
        cleanup_owned_child(ready, script)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descendant ownership checks")
async def test_cancellation_during_startup_cleans_its_child(shell_home):
    script, ready = owned_child(shell_home)
    shell_home.profile.write_text(
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} &\nwait\n",
        encoding="utf-8",
    )
    task = asyncio.create_task(
        execute(
            shell_home.config,
            "bash",
            "printf executed > command-ran",
            timeout=30,
        )
    )
    try:
        pid = await wait_for_child(ready, task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (shell_home.workspace / "command-ran").exists()
        await assert_child_stopped(pid)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        cleanup_owned_child(ready, script)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descendant ownership checks")
@pytest.mark.parametrize("stage", ["startup", "command"])
async def test_completed_command_cleans_children_holding_output_pipes(
    shell_home, stage
):
    script, ready = owned_child(shell_home)
    spawn = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} &\n"
        f"while [ ! -f {shlex.quote(str(ready))} ]; do sleep 0.02; done\n"
    )
    if stage == "startup":
        shell_home.profile.write_text(spawn, encoding="utf-8")
        kind, source = "python", "print('command-ok', end='')"
    else:
        kind, source = "bash", spawn + "printf command-ok"
    try:
        result = await execute(shell_home.config, kind, source, timeout=5)
        assert "error" not in result, result
        assert result["exit_code"] == 0, result
        assert result["output"] == "command-ok"
        assert ready.exists()
        await assert_child_stopped(json.loads(ready.read_text())["pid"])
    finally:
        cleanup_owned_child(ready, script)
