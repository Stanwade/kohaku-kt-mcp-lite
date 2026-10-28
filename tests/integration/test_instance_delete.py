"""Deletion through real entry points, using only temporary owned resources."""

import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from test_tui_parity import (
    OwnedServices,
    available_port,
    catalog_path,
    failure_detail,
    fingerprint,
    open_action,
    read_workspace_marker,
    ready_pid,
    require,
    run_cli,
    terminal,
    terminal_environment,
    wait_for,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="real curses interaction requires an ordinary Linux user",
)

DELETE_ACTION = "Delete instance (remove configuration)"


def create_saved(name, workspace, environment, *, port=None):
    arguments = ["instance", "create", name, "--workspace", str(workspace)]
    if port is not None:
        arguments.extend(["--port", str(port)])
    run_cli(arguments, cwd=workspace.parent, environment=environment)
    if name == "kt-mcp":
        return catalog_path(environment, name).parents[1] / "config.json"
    return catalog_path(environment, name)


def listed_names(cwd, environment):
    return {
        item["name"]
        for item in json.loads(
            run_cli(["instance", "list", "--json"], cwd=cwd, environment=environment)
        )
    }


@pytest.mark.parametrize("entry", ["overview", "details", "explicit", "default"])
def test_native_delete_saved_instance_cancel_and_confirm_at_64x18(tmp_path, entry):
    environment = terminal_environment(config_home=tmp_path / "xdg")
    name = "kt-mcp" if entry == "default" else "kt-mcp-delete-" + secrets.token_hex(4)
    workspace = tmp_path / "retained workspace 中文"
    workspace.mkdir()
    marker = workspace / "keep.txt"
    marker.write_text("retained project content\n", encoding="utf-8")
    port = available_port()
    path = create_saved(name, workspace, environment, port=port)
    before = fingerprint(path)
    arguments = (
        ["tui", "--config", str(path), "--name", name]
        if entry == "explicit"
        else ["tui"]
    )
    with terminal(arguments, cwd=workspace, environment=environment) as session:
        session.expect("Kohaku KT MCP")
        if entry == "details":
            session.expect("Instances")
            session.send("\n")
        if entry in {"details", "explicit"}:
            session.expect("Unit:")
        else:
            session.expect("Selected: " + name)

        open_action(session, DELETE_ACTION)
        session.expect("Delete instance")
        session.expect("No, return")
        session.require_curses()
        # Accept the visible default. A destructive action must not happen.
        session.send("\n")
        session.expect("Unit:" if entry in {"details", "explicit"} else "Instances")
        require(
            fingerprint(path) == before, "Cancelling deletion changed configuration"
        )
        require(
            name in listed_names(tmp_path, environment),
            "Cancelling deletion removed the instance from the catalog",
        )

        open_action(session, DELETE_ACTION)
        session.expect("Delete instance")
        session.choose("Yes, continue")
        session.expect("No instances yet.")
        session.require_curses()
        require(
            not path.exists(), "Native deletion retained the selected configuration"
        )
        require(
            not listed_names(tmp_path, environment),
            "Native deletion left a dangling catalog entry",
        )
        require(
            workspace.is_dir()
            and marker.read_text(encoding="utf-8") == "retained project content\n",
            "Native deletion changed workspace files",
        )
        require(
            not (
                Path(environment["XDG_CONFIG_HOME"])
                / "systemd/user"
                / (name + ".service")
            ).exists(),
            "Deleting a saved-only instance registered a service",
        )
        session.send("q")
        session.finish()

    # Name, workspace and port reservations all become reusable.
    recreated = create_saved(name, workspace, environment, port=port)
    require(recreated == path, "Recreation selected a different configuration path")
    run_cli(
        ["instance", "delete", name, "--yes"],
        cwd=tmp_path,
        environment=environment,
    )
    require(not path.exists(), "CLI deletion did not remove a recreated instance")
    require(
        marker.read_text(encoding="utf-8") == "retained project content\n",
        "CLI deletion changed workspace files",
    )


def test_cli_delete_requires_explicit_noninteractive_confirmation(tmp_path):
    environment = terminal_environment(config_home=tmp_path / "xdg")
    name = "kt-mcp-delete-confirm-" + secrets.token_hex(4)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = create_saved(name, workspace, environment)
    before = fingerprint(path)
    result = subprocess.run(
        [sys.executable, "-m", "kt_mcp", "instance", "delete", name],
        cwd=tmp_path,
        env=environment,
        input="y\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    require(
        result.returncode != 0 and "--yes" in result.stdout + result.stderr,
        "A noninteractive deletion did not require explicit --yes authorization",
    )
    require(
        fingerprint(path) == before,
        "Unconfirmed noninteractive deletion changed the configuration",
    )
    run_cli(
        ["instance", "delete", name, "--yes"], cwd=tmp_path, environment=environment
    )
    require(not path.exists(), "Confirmed noninteractive deletion failed")
    require(workspace.is_dir(), "Deletion removed the workspace")


def test_native_delete_invalid_saved_configuration_at_64x18(tmp_path):
    environment = terminal_environment(config_home=tmp_path / "xdg")
    name = "kt-mcp-delete-invalid-" + secrets.token_hex(4)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = create_saved(name, workspace, environment)
    # Corrupt a test-owned saved file, preserving its private ownership/mode.
    path.write_text("{invalid saved configuration\n", encoding="utf-8")
    with terminal(["tui"], cwd=workspace, environment=environment) as session:
        session.expect("Selected: " + name)
        open_action(session, DELETE_ACTION)
        session.expect("Delete instance")
        session.choose("Yes, continue")
        session.expect("No instances yet.")
        session.require_curses()
        require(
            not path.exists(), "An invalid saved configuration could not be deleted"
        )
        require(
            workspace.is_dir(), "Deleting an invalid configuration removed workspace"
        )
        session.send("q")
        session.finish()


def test_delete_detects_implicit_foreground_daemon_in_its_own_catalog(tmp_path):
    first_environment = terminal_environment(config_home=tmp_path / "first-xdg")
    second_environment = terminal_environment(config_home=tmp_path / "second-xdg")
    first_workspace = tmp_path / "foreground workspace"
    second_workspace = tmp_path / "unrelated workspace"
    first_workspace.mkdir()
    second_workspace.mkdir()
    (first_workspace / "marker.txt").write_text(
        "foreground-stays-online", encoding="utf-8"
    )
    first_path = create_saved("kt-mcp", first_workspace, first_environment)
    before = fingerprint(first_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "kt_mcp", "daemon"],
        cwd=first_workspace,
        env=first_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    failure = None

    def ready():
        if process.poll() is not None:
            return False
        try:
            record = json.loads(
                first_path.with_suffix(".status.json").read_text(encoding="utf-8")
            )
            return record.get("pid") == process.pid
        except (OSError, ValueError):
            return False

    try:
        wait_for(ready, "The isolated implicit foreground daemon did not start")
        asyncio.run(read_workspace_marker(first_path, "foreground-stays-online"))
        second_path = create_saved("kt-mcp", second_workspace, second_environment)
        # Both instances have the default name, but route through distinct XDG roots.
        run_cli(
            ["instance", "delete", "kt-mcp", "--yes"],
            cwd=tmp_path,
            environment=second_environment,
        )
        require(
            not second_path.exists() and process.poll() is None,
            "An unrelated default-name deletion affected the live foreground daemon",
        )
        result = subprocess.run(
            [sys.executable, "-m", "kt_mcp", "instance", "delete", "kt-mcp", "--yes"],
            cwd=tmp_path,
            env=first_environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        require(
            result.returncode != 0 and "foreground" in result.stderr.lower(),
            "Deletion did not refuse a running implicit foreground daemon",
        )
        require(
            process.poll() is None and fingerprint(first_path) == before,
            "Refused deletion changed foreground process or saved configuration",
        )
        asyncio.run(read_workspace_marker(first_path, "foreground-stays-online"))
    except Exception as error:  # noqa: BLE001 -- redact nested HTTP failures
        failure = "Foreground deletion guard failed: " + failure_detail(error)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    if failure:
        pytest.fail(failure, pytrace=False)
    run_cli(
        ["instance", "delete", "kt-mcp", "--yes"],
        cwd=tmp_path,
        environment=first_environment,
    )
    require(
        not first_path.exists(), "The stopped foreground instance could not be deleted"
    )
    require(
        (first_workspace / "marker.txt").read_text(encoding="utf-8")
        == "foreground-stays-online",
        "Deleting the stopped foreground instance changed project files",
    )


@pytest.fixture
def delete_services():
    resources = OwnedServices()
    try:
        yield resources
    finally:
        resources.cleanup()


@pytest.mark.skipif(
    os.environ.get("KT_MCP_TEST_SYSTEMD") != "1",
    reason="set KT_MCP_TEST_SYSTEMD=1 for temporary real-service deletion checks",
)
def test_real_delete_selected_instance_keeps_peer_and_workspace(
    tmp_path, delete_services
):
    from kt_mcp.instances import read_config

    environment = terminal_environment()
    peer, peer_path = delete_services.reserve("delete-peer")
    target, target_path = delete_services.reserve("delete-target")
    peer_workspace = tmp_path / "peer workspace"
    workspace = tmp_path / "deleted instance workspace"
    peer_workspace.mkdir()
    workspace.mkdir()
    (peer_workspace / "marker.txt").write_text("peer-survives-delete", encoding="utf-8")
    marker = workspace / "marker.txt"
    marker.write_text("workspace-survives-delete", encoding="utf-8")
    failure = None
    stage = "starting independent temporary instances"

    def peer_unchanged():
        require(
            ready_pid(peer, peer_path) == peer_pid,
            "Deleting the selected instance changed its peer process",
        )
        asyncio.run(read_workspace_marker(peer_path, "peer-survives-delete"))

    def target_deleted():
        require(
            not target.unit_path.exists()
            and target.status().get("LoadState") == "not-found"
            and target.status().get("MainPID") in {"0", "", None},
            "Deleting the selected instance left its service registered or running",
        )
        require(
            not target_path.exists()
            and not target_path.with_suffix(".status.json").exists(),
            "Deletion left selected configuration or daemon status behind",
        )
        require(
            target.name not in listed_names(tmp_path, environment),
            "Deleted instance remains in the catalog",
        )
        require(
            marker.read_text(encoding="utf-8") == "workspace-survives-delete",
            "Deletion changed project content",
        )
        peer_unchanged()

    try:
        for manager, work in ((peer, peer_workspace), (target, workspace)):
            run_cli(
                [
                    "instance",
                    "create",
                    manager.name,
                    "--workspace",
                    str(work),
                    "--start",
                ],
                cwd=tmp_path,
                environment=environment,
            )
        wait_for(lambda: ready_pid(peer, peer_path), "Temporary peer did not start")
        wait_for(
            lambda: ready_pid(target, target_path), "Temporary target did not start"
        )
        peer_pid = ready_pid(peer, peer_path)
        target_pid = ready_pid(target, target_path)
        target.control("enable")
        before = fingerprint(target_path)
        old_config = read_config(target_path)
        old_port = old_config.settings.port

        stage = "cancelling deletion of the running target through the small TUI"
        with terminal(
            ["tui", "--instance", target.name], cwd=workspace, environment=environment
        ) as session:
            session.expect("Unit:")
            open_action(session, DELETE_ACTION)
            session.expect("Delete instance")
            session.send("\n")
            session.expect("Unit:")
            require(
                ready_pid(target, target_path) == target_pid
                and fingerprint(target_path) == before
                and target.status().get("UnitFileState") == "enabled",
                "Cancelling deletion stopped, disabled or changed the target",
            )
            peer_unchanged()

            stage = "deleting only the running target through the small TUI"
            open_action(session, DELETE_ACTION)
            session.expect("Delete instance")
            session.choose("Yes, continue")
            session.expect("Instances")
            wait_for(
                lambda: not target_path.exists(), "Native deletion did not complete"
            )
            target_deleted()
            session.require_curses()
            session.send("q")
            session.finish()

        stage = "reusing the deleted name, workspace and port"
        run_cli(
            [
                "instance",
                "create",
                target.name,
                "--workspace",
                str(workspace),
                "--port",
                str(old_port),
                "--start",
            ],
            cwd=tmp_path,
            environment=environment,
        )
        wait_for(
            lambda: ready_pid(target, target_path), "Recreated instance did not start"
        )
        require(
            read_config(target_path).token != old_config.token,
            "Recreated instance reused deleted credentials",
        )
        asyncio.run(read_workspace_marker(target_path, "workspace-survives-delete"))
        peer_unchanged()

        stage = "deleting the recreated running instance through CLI"
        run_cli(
            ["instance", "delete", target.name, "--yes"],
            cwd=tmp_path,
            environment=environment,
        )
        target_deleted()
    except Exception as error:  # noqa: BLE001 -- keep private URLs out of failures
        failure = "Instance deletion " + stage + " failed: " + failure_detail(error)
    if failure:
        pytest.fail(failure, pytrace=False)
