"""Real terminal regression coverage for starting a newly saved instance."""

import asyncio
import os
import re
import signal
import sys
import time
from pathlib import Path

import pytest
from test_tui_parity import (
    OwnedServices,
    available_port,
    create_form,
    failure_detail,
    fingerprint,
    ready_pid,
    require,
    terminal,
    terminal_environment,
    wait_for,
)

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "linux" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="real curses interaction requires an ordinary Linux user",
    ),
    pytest.mark.skipif(
        os.environ.get("KT_MCP_TEST_SYSTEMD") != "1",
        reason="set KT_MCP_TEST_SYSTEMD=1 for temporary real-service startup checks",
    ),
]


@pytest.fixture
def owned_services(tmp_path):
    # The tmp_path dependency keeps each workspace available during unit checks
    # and verified service cleanup, including when a terminal assertion fails.
    resources = OwnedServices()
    try:
        yield resources
    finally:
        resources.cleanup()


def delayed_connector(tmp_path):
    """Use actual child-process registration without a public tunnel or network."""
    executable = tmp_path / "delayed-cloudflared"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import time\n"
        "print('https://startup-test.trycloudflare.com', flush=True)\n"
        "time.sleep(8)\n"
        "print('Registered tunnel connection', flush=True)\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def save_and_start(session, manager, workspace, *, connector=None):
    create_form(
        session,
        manager.name,
        workspace,
        available_port(),
        mode="quick" if connector else "local",
    )
    if connector:
        session.send("advanced\n")
        session.enter("Cloudflared executable", str(connector))
        session.enter("Allow file tools outside workspace?", "no")
        session.expect("Save?")
    session.send("\n")
    session.expect("Start instance now?")
    session.choose("Yes, continue")


def elapsed_line(session):
    match = re.search(r"Elapsed[^\n]*", session.screen.text)
    return match.group(0) if match else ""


def require_live_progress(session):
    session.expect("Starting instance", timeout=5)
    session.expect("Elapsed", timeout=5)
    initial = elapsed_line(session)
    require(bool(initial), "The startup screen omitted elapsed time")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        session.pump(0.1)
        if elapsed_line(session) and elapsed_line(session) != initial:
            session.require_curses()
            return
    require(False, "The startup progress screen stopped updating")


def ui_thread_count(session):
    return len(list((Path("/proc") / str(session.process.pid) / "task").iterdir()))


async def read_local_marker(path, expected):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from kt_mcp.instances import read_config

    config = read_config(path)
    url = f"http://127.0.0.1:{config.settings.port}/mcp/{config.token}"
    async with (
        httpx.AsyncClient(trust_env=False, timeout=10) as http,
        streamable_http_client(url, http_client=http) as (reader, writer, _),
        ClientSession(reader, writer) as client,
    ):
        await client.initialize()
        result = await client.call_tool("read", {"path": "marker.txt"})
        require(
            not result.isError
            and expected in result.structuredContent.get("output", ""),
            "The started service did not serve its selected workspace",
        )


def test_new_instance_yes_start_returns_ready_to_overview(tmp_path, owned_services):
    manager, path = owned_services.reserve("yes-local")
    workspace = tmp_path / "new local workspace"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("yes-start-local", encoding="utf-8")
    environment = terminal_environment()
    linger_before = manager.linger_status()
    failure = None
    try:
        with terminal(["tui"], cwd=tmp_path, environment=environment) as session:
            session.expect("Instances")
            original_threads = ui_thread_count(session)
            save_and_start(session, manager, workspace)
            session.expect("Instances", timeout=20)
            session.expect("Selected: " + manager.name)
            require(
                bool(ready_pid(manager, path)), "Yes Start returned before readiness"
            )
            require(
                manager.status().get("UnitFileState") == "disabled"
                and manager.linger_status() == linger_before,
                "Yes Start changed automatic startup or linger",
            )
            wait_for(
                lambda: ui_thread_count(session) == original_threads,
                "The completed startup retained a UI worker",
                timeout=3,
            )
            asyncio.run(read_local_marker(path, "yes-start-local"))
            session.send("q")
            session.finish()
        require(
            bool(ready_pid(manager, path)), "Exiting the manager stopped the service"
        )
    except Exception as error:  # noqa: BLE001 -- omit credential-bearing transcripts
        failure = "New-instance Yes Start failed: " + failure_detail(error)
    if failure:
        pytest.fail(failure, pytrace=False)


def test_slow_new_instance_start_updates_progress_until_ready(tmp_path, owned_services):
    manager, path = owned_services.reserve("yes-slow")
    workspace = tmp_path / "slow startup workspace"
    workspace.mkdir()
    environment = terminal_environment()
    connector = delayed_connector(tmp_path)
    failure = None
    try:
        with terminal(["tui"], cwd=tmp_path, environment=environment) as session:
            session.expect("Instances")
            original_threads = ui_thread_count(session)
            save_and_start(session, manager, workspace, connector=connector)
            require_live_progress(session)
            require(
                manager.status().get("ActiveState") == "active",
                "The controlled slow service did not start",
            )
            require(
                not ready_pid(manager, path),
                "The controlled slow connector became ready before progress was tested",
            )
            session.expect("Instances", timeout=20)
            require(
                bool(ready_pid(manager, path)), "Startup completion was not verified"
            )
            wait_for(
                lambda: ui_thread_count(session) == original_threads,
                "The completed slow startup retained a UI worker",
                timeout=3,
            )
            session.send("q")
            session.finish()
    except Exception as error:  # noqa: BLE001 -- omit credential-bearing transcripts
        failure = "Slow new-instance startup failed: " + failure_detail(error)
    if failure:
        pytest.fail(failure, pytrace=False)


@pytest.mark.parametrize("cancel", ["q", "escape", "interrupt"])
def test_startup_wait_can_end_while_the_service_continues(
    tmp_path, owned_services, cancel
):
    manager, path = owned_services.reserve("cancel-" + cancel)
    workspace = tmp_path / "continuing startup workspace"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("startup-continues", encoding="utf-8")
    environment = terminal_environment()
    connector = delayed_connector(tmp_path)
    failure = None
    try:
        with terminal(["tui"], cwd=tmp_path, environment=environment) as session:
            session.expect("Instances")
            original_threads = ui_thread_count(session)
            save_and_start(session, manager, workspace, connector=connector)
            require_live_progress(session)
            before = fingerprint(path)
            current = manager.status()
            require(
                current.get("ActiveState") == "active" and not ready_pid(manager, path),
                "The controlled service was not waiting for startup verification",
            )
            active_pid = current.get("MainPID")
            if cancel == "interrupt":
                # A user's Ctrl+C sends SIGINT from the controlling terminal.
                os.kill(session.process.pid, signal.SIGINT)
            else:
                session.send("q" if cancel == "q" else "\x1b")
            session.expect("Instances", timeout=4)
            session.expect("Startup wait ended")
            session.require_curses()
            wait_for(
                lambda: ui_thread_count(session) == original_threads,
                "Ending startup wait retained a UI worker",
                timeout=3,
            )
            after = manager.status()
            require(
                after.get("ActiveState") == "active"
                and after.get("MainPID") == active_pid
                and fingerprint(path) == before,
                "Ending startup wait changed the running service or configuration",
            )
            wait_for(
                lambda: ready_pid(manager, path),
                "The service did not finish startup after ending the UI wait",
                timeout=20,
            )
            asyncio.run(read_local_marker(path, "startup-continues"))
            session.send("q")
            session.finish()
    except Exception as error:  # noqa: BLE001 -- omit credential-bearing transcripts
        failure = "Ending startup wait failed: " + failure_detail(error)
    if failure:
        pytest.fail(failure, pytrace=False)
