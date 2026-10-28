"""Opt-in multi-instance checks against an ordinary user's real systemd manager.

Only randomly named kt-mcp-it-* services and their exact configuration files are
created or removed. The user's default kt-mcp service is never controlled.
"""

import asyncio
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kt_mcp.daemon import (
    DaemonConfig,
    client_url,
    default_config_path,
    load_config,
    save_config,
)
from kt_mcp.desktop import DesktopSettings

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or os.environ.get("KT_MCP_TEST_SYSTEMD") != "1"
    or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="set KT_MCP_TEST_SYSTEMD=1 as an ordinary Linux user with user systemd",
)


class InstanceCheckFailed(AssertionError):
    """A static test failure that contains no configuration or URL values."""


def require(condition, message):
    # Ordinary exceptions pass through anyio task groups to the redacted report.
    if not condition:
        raise InstanceCheckFailed(message)


def failure_detail(error):
    children = getattr(error, "exceptions", ())
    if children:
        return "; ".join(failure_detail(child) for child in children)
    if isinstance(error, InstanceCheckFailed):
        return str(error)
    return type(error).__name__ + " (credential-bearing details omitted)"


def snapshot(path):
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(
        path.stat().st_mode
    )


def service_state(manager):
    status = manager.status()
    return {
        key: status.get(key)
        for key in ("LoadState", "ActiveState", "MainPID", "UnitFileState")
    }


def wait_ready(manager, config_path, *, previous_pid=None, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.status()
        try:
            record = json.loads(
                config_path.with_suffix(".status.json").read_text(encoding="utf-8")
            )
            pid = int(status.get("MainPID", "0"))
            if (
                status.get("ActiveState") == "active"
                and pid > 0
                and pid != previous_pid
                and record.get("pid") == pid
            ):
                return pid
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    require(False, "A temporary instance did not become ready")


def quit_overview_in_terminal(names):
    """Exercise the actual unified CLI entry without displaying captured links."""
    import fcntl
    import pty
    import re
    import select
    import struct
    import subprocess
    import termios

    master = slave = None
    process = None
    try:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 48, 180, 0, 0))
        process = subprocess.Popen(
            [sys.executable, "-m", "kt_mcp", "tui"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={
                **os.environ,
                "TERM": "xterm-256color",
                "LC_ALL": "C.UTF-8",
                "NO_COLOR": "1",
            },
            start_new_session=True,
        )
        os.close(slave)
        slave = None
        output = bytearray()
        ready = False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
                plain = re.sub(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\([0-~])", b"", output)
                ready = b"Instances" in plain and all(
                    name.encode("utf-8") in plain for name in names
                )
                if ready:
                    break
            if process.poll() is not None:
                break
        require(ready, "The unified terminal overview did not list both live instances")
        os.write(master, b"q")
        require(
            process.wait(timeout=10) == 0, "The instance overview did not quit cleanly"
        )
    finally:
        try:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        finally:
            if slave is not None:
                os.close(slave)
            if master is not None:
                os.close(master)


@asynccontextmanager
async def connect(url):
    async with (
        httpx.AsyncClient(trust_env=False, timeout=10) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def tool(session, name, arguments, *, error=False):
    result = await session.call_tool(name, arguments)
    require(bool(result.isError) is error, "Unexpected MCP tool result for " + name)
    if error:
        return None
    require(
        isinstance(result.structuredContent, dict),
        "MCP tool did not return a structured result",
    )
    return result.structuredContent


async def assert_marker(session, expected):
    result = await tool(session, "read", {"path": "shared-name.txt"})
    require(
        expected in result.get("output", ""), "An instance read the wrong workspace"
    )


def require_same_running_instance(manager, expected_pid):
    current = manager.status()
    require(
        current.get("ActiveState") == "active"
        and current.get("MainPID") == str(expected_pid),
        "An action on one instance changed the other instance's process",
    )


def clean_exact_instances(managers, paths):
    """Keep configs for any unit that could not safely be uninstalled."""
    from kt_mcp.managed_service import inspect_service

    failures = []
    retained = set()
    for manager, path in reversed(managers):
        if not manager.unit_path.exists():
            continue
        try:
            binding = inspect_service(
                manager.name,
                False,
                expected_python=sys.executable,
                expected_config=path,
            )
            if binding is None:
                raise RuntimeError("temporary unit binding could not be verified")
            manager.uninstall()
        except Exception:  # noqa: BLE001 -- cleanup must attempt every owned unit
            failures.append(manager.name)
            retained.add(path)
    for path in paths:
        if path in retained:
            continue
        try:
            path.with_suffix(".status.json").unlink(missing_ok=True)
            path.unlink(missing_ok=True)
        except OSError:
            failures.append(path.name)
    require(
        not failures,
        "Temporary instance cleanup failed; inspect only: " + ", ".join(failures),
    )


async def test_real_instances_isolate_tools_tokens_and_configuration(
    tmp_path, monkeypatch, capsys
):
    from kt_mcp import instances
    from kt_mcp.service import ServiceManager
    from kt_mcp.tui import TerminalController

    for variable in ("KT_MCP_TOKEN", "TUNNEL_TOKEN"):
        monkeypatch.delenv(variable, raising=False)

    run_name = "kt-mcp-it-" + secrets.token_hex(6)
    owned_paths = []
    managers = []
    stage = "setup"
    failure = None
    default_manager = ServiceManager()
    original_default_state = service_state(default_manager)
    original_default_config = snapshot(default_config_path())
    original_default_unit = snapshot(default_manager.unit_path)

    def reserve(suffix):
        name = run_name + "-" + suffix
        path = instances.config_path(name)
        require(not path.exists(), "A randomly named test configuration already exists")
        owned_paths.append(path)
        return name, path

    def expect_conflict(name, **options):
        try:
            instances.create_instance(name, **options)
        except instances.InstanceError:
            return
        require(False, "A conflicting instance was accepted")

    try:
        work_a = tmp_path / "workspace alpha 中文"
        work_b = tmp_path / "workspace beta"
        work_c = tmp_path / "workspace alpha changed"
        work_named = tmp_path / "named config only"
        work_conflict = tmp_path / "conflict candidate"
        for workspace in (work_a, work_b, work_c, work_named, work_conflict):
            workspace.mkdir()
        name_a, path_a = reserve("a")
        name_b, path_b = reserve("b")
        # Creation must generate independent saved tokens, even when the caller
        # has a protocol-launch token in its environment.
        inherited_token = secrets.token_urlsafe(32)
        with monkeypatch.context() as environment:
            environment.setenv("KT_MCP_TOKEN", inherited_token)
            item_a = instances.create_instance(name_a, workspace=work_a)
            item_b = instances.create_instance(name_b, workspace=work_b)
        config_a, config_b = load_config(path_a), load_config(path_b)
        all_tokens = (config_a.token, config_b.token)
        require(
            len(set(all_tokens)) == 2 and inherited_token not in all_tokens,
            "Named instances did not receive independent saved credentials",
        )
        require(item_a.port != item_b.port, "Named instances were assigned one port")
        require(
            item_a.config_path == path_a and item_b.config_path == path_b,
            "The catalog returned the wrong configuration path",
        )
        listed = {item.name: item for item in instances.list_instances()}
        require(
            name_a in listed and name_b in listed,
            "The instance catalog omitted a created instance",
        )
        require(
            instances.get_instance(name_a).config_path == path_a
            and instances.find_workspace(work_b).name == name_b,
            "Named instance or workspace selection resolved the wrong configuration",
        )
        for path in (path_a, path_b):
            require(
                stat.S_IMODE(path.stat().st_mode) == 0o600,
                "An instance configuration is not private",
            )

        stage = "registering temporary services"
        manager_a, manager_b = ServiceManager(name_a), ServiceManager(name_b)
        for manager, path in ((manager_a, path_a), (manager_b, path_b)):
            require(not manager.unit_path.exists(), "A random test unit already exists")
            managers.append((manager, path))
            await asyncio.to_thread(
                manager.install, path, now=True, enable=False, linger=False
            )
        pid_a = await asyncio.to_thread(wait_ready, manager_a, path_a)
        pid_b = await asyncio.to_thread(wait_ready, manager_b, path_b)
        url_a, url_b = client_url(config_a, path_a), client_url(config_b, path_b)
        require(url_a != url_b, "Independent instances returned one MCP link")

        stage = "opening and quitting the unified terminal overview"
        await asyncio.to_thread(quit_overview_in_terminal, (name_a, name_b))
        require_same_running_instance(manager_a, pid_a)
        require_same_running_instance(manager_b, pid_b)

        stage = "real MCP file and authentication isolation"
        async with connect(url_a) as session_a, connect(url_b) as session_b:
            await tool(
                session_a,
                "write",
                {"path": "shared-name.txt", "content": "alpha-only"},
            )
            await tool(
                session_b,
                "write",
                {"path": "shared-name.txt", "content": "beta-only"},
            )
            await assert_marker(session_a, "alpha-only")
            await assert_marker(session_b, "beta-only")
            await tool(
                session_a,
                "read",
                {"path": str(work_b / "shared-name.txt")},
                error=True,
            )
            await tool(
                session_b,
                "read",
                {"path": str(work_a / "shared-name.txt")},
                error=True,
            )
        require(
            (work_a / "shared-name.txt").read_text() == "alpha-only"
            and (work_b / "shared-name.txt").read_text() == "beta-only",
            "MCP writes were not isolated to their selected workspaces",
        )
        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            wrong_a = await http.get(
                f"http://127.0.0.1:{item_a.port}/mcp/{config_b.token}"
            )
            wrong_b = await http.get(
                f"http://127.0.0.1:{item_b.port}/mcp/{config_a.token}"
            )
            require(
                wrong_a.status_code == wrong_b.status_code == 404,
                "One instance accepted the other instance's credential",
            )

        stage = "rejecting port, workspace, and public-origin conflicts"
        conflict_name, conflict_path = reserve("port-conflict")
        expect_conflict(conflict_name, workspace=work_conflict, port=item_a.port)
        require(not conflict_path.exists(), "A rejected port conflict saved a config")
        conflict_name, conflict_path = reserve("workspace-conflict")
        expect_conflict(conflict_name, workspace=work_a)
        require(
            not conflict_path.exists(), "A rejected workspace conflict saved a config"
        )
        named_name, named_path = reserve("named")
        origin = f"https://{run_name}.example"
        # No public tunnel is started: this tests origin reservation in saved
        # configurations without requiring Cloudflare credentials or a network.
        instances.create_instance(
            named_name,
            workspace=work_named,
            mode="named",
            public_origin=origin,
            tunnel_token=secrets.token_urlsafe(32),
        )
        conflict_name, conflict_path = reserve("origin-conflict")
        expect_conflict(
            conflict_name,
            workspace=work_conflict,
            mode="named",
            public_origin=origin,
            tunnel_token=secrets.token_urlsafe(32),
        )
        require(not conflict_path.exists(), "A rejected origin conflict saved a config")
        require(named_path.exists(), "The original named configuration was lost")
        require_same_running_instance(manager_a, pid_a)
        require_same_running_instance(manager_b, pid_b)

        stage = "configuring one instance while the other serves requests"
        controller = TerminalController(manager_a, path_a)
        before_edit = path_a.read_bytes()
        answers = iter(["y", str(work_c), "", "local", ""])
        questions = []

        def configure_answer(prompt):
            questions.append(prompt)
            if len(questions) == 1:
                require_same_running_instance(manager_a, pid_a)
                require("y/N" in prompt, "The stop confirmation must default to No")
            else:
                state = manager_a.status()
                require(
                    state.get("ActiveState") in {"inactive", "failed"}
                    and state.get("MainPID") == "0",
                    "Configuration questions were asked before this instance stopped",
                )
                require(
                    path_a.read_bytes() == before_edit,
                    "Configuration was saved before the final review",
                )
            require_same_running_instance(manager_b, pid_b)
            return next(answers)

        async with connect(url_b) as session_b:
            await asyncio.to_thread(controller.configure, ask=configure_answer)
            require(len(questions) >= 5, "The configuration wizard did not complete")
            require(
                manager_a.status().get("MainPID") == "0",
                "Saving configuration automatically restarted the selected instance",
            )
            changed = load_config(path_a)
            require(
                changed.settings.workspace == str(work_c)
                and changed.token == config_a.token,
                "Configuration did not update only the workspace while keeping its token",
            )
            require(
                instances.find_workspace(work_c).name == name_a,
                "Workspace selection still used stale catalog data after configuration",
            )
            await assert_marker(session_b, "beta-only")
            require_same_running_instance(manager_b, pid_b)
            await asyncio.to_thread(controller.act, "s")
            second_pid_a = await asyncio.to_thread(
                wait_ready, manager_a, path_a, previous_pid=pid_a
            )
            async with connect(url_a) as session_a:
                await tool(session_a, "read", {"path": "shared-name.txt"}, error=True)
                await tool(
                    session_a,
                    "write",
                    {"path": "shared-name.txt", "content": "alpha-new-workspace"},
                )
                await assert_marker(session_a, "alpha-new-workspace")
            require(
                (work_a / "shared-name.txt").read_text() == "alpha-only",
                "Reconfiguration modified the previous workspace",
            )
            await assert_marker(session_b, "beta-only")

            stage = "cancelling configuration after the confirmed stop"
            before_cancel = snapshot(path_a), snapshot(manager_a.unit_path)
            cancellation = iter(["y", ":q"])
            await asyncio.to_thread(
                controller.configure, ask=lambda prompt: next(cancellation)
            )
            require(
                before_cancel == (snapshot(path_a), snapshot(manager_a.unit_path)),
                "Cancelling configuration changed saved settings or the unit",
            )
            require(
                manager_a.status().get("MainPID") == "0",
                "Cancelling a confirmed configuration stop restarted the instance",
            )
            await assert_marker(session_b, "beta-only")
            require_same_running_instance(manager_b, pid_b)

            # Selecting Start is the only step that brings this instance online.
            await asyncio.to_thread(controller.act, "s")
            await asyncio.to_thread(
                wait_ready, manager_a, path_a, previous_pid=second_pid_a
            )
            await assert_marker(session_b, "beta-only")
            require_same_running_instance(manager_b, pid_b)

        stage = "stopping only the other selected instance"
        last_pid_a = int(manager_a.status()["MainPID"])
        await asyncio.to_thread(TerminalController(manager_b, path_b).act, "x")
        require(
            manager_b.status().get("MainPID") == "0", "Stop did not stop its target"
        )
        require_same_running_instance(manager_a, last_pid_a)
        async with connect(url_a) as session_a:
            await assert_marker(session_a, "alpha-new-workspace")

        stage = "checking default compatibility and credential-free diagnostics"
        require(
            snapshot(default_config_path()) == original_default_config
            and snapshot(default_manager.unit_path) == original_default_unit
            and service_state(default_manager) == original_default_state,
            "Named-instance operations changed the existing default instance",
        )
        captured = capsys.readouterr()
        diagnostics = captured.out + captured.err
        for manager, _ in managers:
            diagnostics += manager.unit_path.read_text() + manager.logs()
        require(
            all(token not in diagnostics for token in (*all_tokens, inherited_token)),
            "Multi-instance operations exposed a credential in diagnostics",
        )
    except Exception as exc:  # noqa: BLE001 -- never print credential-bearing traces
        failure = f"Multi-instance {stage} failed: {failure_detail(exc)}"
    finally:
        await asyncio.to_thread(clean_exact_instances, managers, owned_paths)
    require(
        service_state(default_manager) == original_default_state
        and snapshot(default_config_path()) == original_default_config
        and snapshot(default_manager.unit_path) == original_default_unit,
        "Temporary instance cleanup changed the existing default instance",
    )
    if failure:
        # Raise outside the exception handler so nested HTTP/MCP task-group
        # tracebacks cannot expose a private connection URL via exception chaining.
        pytest.fail(failure, pytrace=False)


def test_legacy_default_and_named_catalog_coexist_without_rewriting_default(
    tmp_path, monkeypatch
):
    from kt_mcp import instances

    for variable in ("KT_MCP_TOKEN", "TUNNEL_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    legacy_path = tmp_path / "private-config/kt-mcp/config.json"
    monkeypatch.setattr(instances, "default_config_path", lambda: legacy_path)
    legacy_workspace = tmp_path / "legacy workspace"
    named_workspace = tmp_path / "named workspace"
    legacy_workspace.mkdir()
    named_workspace.mkdir()
    legacy = DaemonConfig(
        DesktopSettings(str(legacy_workspace), 28765, "local"),
        secrets.token_urlsafe(32),
    )
    save_config(legacy, legacy_path)
    before = snapshot(legacy_path)
    named = instances.create_instance("isolated-project", workspace=named_workspace)
    names = {item.name for item in instances.list_instances()}
    require(
        {"kt-mcp", "kt-mcp-isolated-project"} <= names,
        "The legacy default and named configuration were not both discovered",
    )
    require(
        instances.get_instance("kt-mcp").config_path == legacy_path
        and instances.get_instance("isolated-project").config_path == named.config_path,
        "Default or named instance selection used the wrong configuration",
    )
    require(
        instances.find_workspace(legacy_workspace).name == "kt-mcp"
        and instances.find_workspace(named_workspace).name == named.name,
        "Workspace lookup did not preserve legacy default compatibility",
    )
    require(
        snapshot(legacy_path) == before
        and load_config(named.config_path).token != legacy.token,
        "Creating a named instance rewrote or reused the legacy default credential",
    )
