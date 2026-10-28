"""Process routing regressions using isolated children with no service or socket."""

import os
import select
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from kt_mcp import instance_delete as deletion
from kt_mcp import instances

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux /proc daemon routing"
)

# Exercise the actual daemon argument parser and path expansion, then block at
# the runtime boundary. The child never loads credentials or starts a server.
_CHILD = """
import sys
from pathlib import Path
from kt_mcp import daemon

def hold(path):
    print(str(Path(path).expanduser().absolute()), flush=True)
    sys.stdin.read()
    return 0

daemon.run_daemon = hold
raise SystemExit(daemon.main(sys.argv[2:]))
"""


@contextmanager
def routed_child(environment, directory, arguments):
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD, "kt_mcp", "daemon", *arguments],
        cwd=directory,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        readable, _, _ = select.select([process.stdout], [], [], 10)
        assert readable, "Isolated daemon routing child did not become ready"
        resolved = process.stdout.readline().strip()
        assert resolved, "Isolated daemon routing child exited before path resolution"
        yield process, Path(resolved)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


@pytest.mark.parametrize(
    "route",
    ["implicit-xdg", "implicit-home", "relative-xdg", "relative", "tilde"],
)
def test_live_daemon_uses_its_own_config_routing_not_the_deleting_environment(
    tmp_path, monkeypatch, route
):
    parent_home = tmp_path / "parent-home"
    child_home = tmp_path / "child-home"
    child_directory = tmp_path / "child-cwd"
    for directory in (parent_home, child_home, child_directory):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(parent_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "parent-xdg"))

    environment = dict(os.environ)
    environment["HOME"] = str(child_home)
    environment.pop("XDG_CONFIG_HOME", None)
    environment.pop("KT_MCP_TOKEN", None)
    environment.pop("TUNNEL_TOKEN", None)
    arguments = []
    if route == "implicit-xdg":
        environment["XDG_CONFIG_HOME"] = str(tmp_path / "child-xdg")
        expected = tmp_path / "child-xdg/kt-mcp/config.json"
    elif route == "relative":
        arguments = ["--config", "selected.json"]
        expected = child_directory / "selected.json"
    elif route == "tilde":
        arguments = ["--config=~/selected.json"]
        expected = child_home / "selected.json"
    else:
        if route == "relative-xdg":
            environment["XDG_CONFIG_HOME"] = "relative-ignored"
        expected = child_home / ".config/kt-mcp/config.json"

    with routed_child(environment, child_directory, arguments) as (process, resolved):
        assert resolved == expected
        assert process.pid in deletion._matching_daemons(expected, os.geteuid())
        assert process.pid not in deletion._matching_daemons(
            tmp_path / "unrelated.json", os.geteuid()
        )
        with pytest.raises(instances.InstanceError, match="foreground"):
            deletion._assert_no_foreground(expected, os.geteuid())
        assert process.poll() is None


def test_live_daemon_protects_shared_status_from_other_config_suffix(tmp_path):
    selected = tmp_path / "work.json"
    foreground = tmp_path / "work.cfg"
    assert selected.with_suffix(".status.json") == foreground.with_suffix(
        ".status.json"
    )
    environment = dict(os.environ)
    environment.pop("KT_MCP_TOKEN", None)
    environment.pop("TUNNEL_TOKEN", None)
    with routed_child(environment, tmp_path, ["--config", str(foreground)]) as (
        process,
        resolved,
    ):
        assert resolved == foreground
        assert process.pid in deletion._matching_daemons(selected, os.geteuid())
        with pytest.raises(instances.InstanceError, match="foreground"):
            deletion._assert_no_foreground(selected, os.geteuid())
        assert process.poll() is None
