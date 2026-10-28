"""Opt-in deletion of one root-managed instance while an ordinary peer stays live."""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from test_tui_parity import (
    available_port,
    failure_detail,
    read_workspace_marker,
    ready_pid,
    require,
    terminal_environment,
    wait_for,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or not hasattr(os, "geteuid")
    or os.geteuid() != 0
    or os.environ.get("KT_MCP_TEST_SYSTEMD") != "1"
    or not os.environ.get("KT_MCP_SYSTEM_TEST_USER"),
    reason="requires root, KT_MCP_TEST_SYSTEMD=1 and an ordinary system test account",
)


def test_system_deletion_keeps_ordinary_account_peer_and_workspace(monkeypatch):
    import pwd

    from kt_mcp.daemon import DaemonConfig, save_config
    from kt_mcp.desktop import DesktopSettings
    from kt_mcp.managed_service import _parse_unit
    from kt_mcp.service import ServiceManager

    account = pwd.getpwnam(os.environ["KT_MCP_SYSTEM_TEST_USER"])
    require(account.pw_uid != 0, "The system test account must be ordinary")
    runuser = shutil.which("runuser")
    if runuser is None:
        pytest.skip("runuser is unavailable for the ordinary peer")
    suffix = secrets.token_hex(6)
    target = ServiceManager("kt-mcp-system-delete-" + suffix, system=True)
    peer_name = "kt-mcp-system-delete-peer-" + suffix
    peer_unit = Path(account.pw_dir) / ".config/systemd/user" / (peer_name + ".service")
    require(
        not target.unit_path.exists() and not peer_unit.exists(),
        "A random system deletion test resource already exists",
    )
    with tempfile.TemporaryDirectory(prefix="kt-mcp-system-delete-") as directory:
        base = Path(directory)
        os.chown(base, account.pw_uid, account.pw_gid)
        root_xdg = base / "unused-root-catalog"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(root_xdg))
        environment = terminal_environment()
        user_environment = {
            **environment,
            "HOME": account.pw_dir,
            "USER": account.pw_name,
            "LOGNAME": account.pw_name,
            "XDG_CONFIG_HOME": str(Path(account.pw_dir) / ".config"),
            "XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{account.pw_uid}/bus",
        }

        def user_cli(arguments):
            result = subprocess.run(
                [
                    runuser,
                    "-u",
                    account.pw_name,
                    "--",
                    sys.executable,
                    "-m",
                    "kt_mcp",
                    *arguments,
                ],
                cwd=base,
                env=user_environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=50,
                check=False,
            )
            require(
                result.returncode == 0, "An isolated ordinary peer operation failed"
            )
            return result.stdout

        def create_config(label):
            workspace = base / (label + " workspace")
            workspace.mkdir()
            os.chown(workspace, account.pw_uid, account.pw_gid)
            marker = workspace / "marker.txt"
            marker.write_text(label + "-retained", encoding="utf-8")
            os.chown(marker, account.pw_uid, account.pw_gid)
            path = base / (label + ".json")
            save_config(
                DaemonConfig(
                    DesktopSettings(str(workspace), available_port(), "local"),
                    secrets.token_urlsafe(32),
                ),
                path,
            )
            os.chown(path, account.pw_uid, account.pw_gid)
            return path, workspace

        peer_path, _ = create_config("peer")
        target_path = None
        failure = None
        try:
            user_cli(
                [
                    "service",
                    "install",
                    "--name",
                    peer_name,
                    "--config",
                    str(peer_path),
                    "--now",
                ]
            )

            def peer_state():
                return json.loads(user_cli(["service", "status", "--name", peer_name]))

            def peer_ready():
                state = peer_state()
                try:
                    record = json.loads(
                        peer_path.with_suffix(".status.json").read_text()
                    )
                except (OSError, ValueError):
                    return None
                pid = int(state.get("MainPID") or 0)
                return pid if pid and record.get("pid") == pid else None

            wait_for(peer_ready, "The ordinary peer did not become ready")
            peer_pid = peer_ready()
            asyncio.run(read_workspace_marker(peer_path, "peer-retained"))
            target_path, workspace = create_config("system")
            target.install(target_path, run_as=account.pw_name, now=True)
            wait_for(
                lambda: ready_pid(target, target_path),
                "The system instance did not start",
            )
            asyncio.run(read_workspace_marker(target_path, "system-retained"))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kt_mcp",
                    "instance",
                    "delete",
                    target.name,
                    "--system",
                    "--config",
                    str(target_path),
                    "--yes",
                ],
                cwd=base,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=50,
                check=False,
            )
            require(result.returncode == 0, "Public CLI system deletion failed")
            require(
                not target.unit_path.exists()
                and target.status().get("LoadState") == "not-found"
                and not target_path.exists()
                and not target_path.with_suffix(".status.json").exists(),
                "System deletion left service, configuration or status behind",
            )
            require(
                (workspace / "marker.txt").read_text(encoding="utf-8")
                == "system-retained",
                "System deletion changed workspace files",
            )
            require(
                not root_xdg.exists(), "System deletion created a root user catalog"
            )
            require(
                peer_ready() == peer_pid,
                "System deletion changed the ordinary peer PID",
            )
            asyncio.run(read_workspace_marker(peer_path, "peer-retained"))
        except Exception as error:  # noqa: BLE001 -- redact private nested HTTP errors
            failure = "System deletion failed: " + failure_detail(error)
        finally:
            if target.unit_path.exists():
                _, _, bound_path = _parse_unit(target.unit_path.read_bytes(), True)
                require(
                    bound_path == target_path,
                    "The temporary system unit binding changed",
                )
                target.uninstall()
            if peer_unit.exists():
                _, _, bound_path = _parse_unit(peer_unit.read_bytes(), False)
                require(
                    bound_path == peer_path, "The temporary peer unit binding changed"
                )
                user_cli(["service", "uninstall", "--name", peer_name])
        require(
            not target.unit_path.exists() and not peer_unit.exists(),
            "Temporary system deletion resources were not removed",
        )
        if failure:
            pytest.fail(failure, pytrace=False)
